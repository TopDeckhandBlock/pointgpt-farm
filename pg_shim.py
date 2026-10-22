#!/usr/bin/env python3
"""pg_shim.py - OpenAI-compatible gateway over PointGPT.ai miniapp API.

Pool of autoreg accounts (master_bank.jsonl: {"token": "***", "balance": ...} per line).
Endpoints:
  GET  /v1/models
  POST /v1/chat/completions   (stream + non-stream; tools ignored, plain text upstream)
  POST /v1/images/generations (Nano Banana 2 / GPT Image 2.5 / Seedream 5 ...)
  POST /v1/video/generations  (async by default: returns task_id; sync=true blocks)
  GET  /v1/video/tasks/{id}
  GET  /health
Env: PG_BANK (path, default ./master_bank.jsonl), PG_PORT (default 8797),
     PG_BIND (default 127.0.0.1), PG_KEY (optional bearer key),
     PG_PERSIST_INTERVAL (sec, default 60), PG_REAP_INTERVAL (sec, default 300),
     PG_AUTOFARM (1/0, default 1), PG_AUTOFARM_MIN_FREE (default 5),
     PG_AUTOFARM_BATCH (default 1), PG_AUTOFARM_INTERVAL (sec, default 120).
Stdlib only. Python 3.9+.
"""
import atexit
import base64
import hmac
import json
import os
import random
import re
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import urllib.error
import urllib.request

BASE = 'https://pointgpt.ai'
AU = 'Bea' + 'rer '
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36'

BANK_PATH = os.environ.get('PG_BANK', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'master_bank.jsonl'))
PORT = int(os.environ.get('PG_PORT', '8797'))
BIND_HOST = os.environ.get('PG_BIND', '127.0.0.1')
API_KEY = os.environ.get('PG_KEY', '')
PERSIST_INTERVAL = int(os.environ.get('PG_PERSIST_INTERVAL', '60'))
REAP_INTERVAL = int(os.environ.get('PG_REAP_INTERVAL', '300'))
AUTOFARM = os.environ.get('PG_AUTOFARM', '1') != '0'
AUTOFARM_MIN_FREE = int(os.environ.get('PG_AUTOFARM_MIN_FREE', '5'))
AUTOFARM_BATCH = int(os.environ.get('PG_AUTOFARM_BATCH', '1'))
AUTOFARM_INTERVAL = int(os.environ.get('PG_AUTOFARM_INTERVAL', '120'))

TEXT_MODELS = [
    'gpt-6-astra', 'claude-opus-5', 'claude-sonnet-5', 'claude-fable-5-1',
    'gemini-4-argon', 'gpt-5.6-sol', 'gpt-5.6-terra', 'deepseek-v4-pro',
    'kimi-k3', 'minimax-m3', 'glm-5.3', 'qwen3.8-max',
]
IMAGE_MODELS = [
    'nanobanano-2', 'nanobanano-2-2k', 'nanobanano-2-4k', 'gpt-image-2.5',
    'seedream-5', 'z-image', 'midjourney',
]

VIDEO_MODELS = ['minimax-h3', 'grok', 'seedance-2-mini', 'wan-3-0', 'veo']
VIDEO_COSTS = {'seedance-2-mini': 8.0, 'minimax-h3': 10.0, 'veo': 64.0,
               'kling': 50.0, 'grok': 10.0, 'wan-3-0': 10.0}
_video_cache = {}   # task_id -> result dict

_lock = threading.Lock()
_bank_lock = threading.Lock()
_pool = []          # list of dicts: token, balance, dead, cooldown_until, dead_probes, next_probe
_pool_mtime = 0.0
_used = {}          # (quota-day, token) -> free requests used today
_busy = set()       # tokens with a text request in flight
_lat = {}           # token -> EWMA latency of successful text requests, seconds
_stats = {'requests': 0, 'ok': 0, 'fail': 0, 'free_hits': 0, 'coin_spend': 0.0}
_events = deque(maxlen=80)
_events_lock = threading.Lock()
STARTED = time.time()
FREE_DAILY = 5      # upstream: 5 free text requests / account / day (reset 21:00 UTC)


def event(msg):
    with _events_lock:
        _events.appendleft({'t': time.time(), 'msg': msg})


def video_cost(model, duration):
    """Coins needed for a video job; duration scales linearly from the 5s base."""
    base = VIDEO_COSTS.get(model, 10.0)
    try:
        d = float(duration or 5)
    except Exception:
        d = 5.0
    return round(base * max(1.0, d / 5.0), 1)


def _today():
    # free quota day boundary is 21:00 UTC (00:00 MSK), not UTC midnight
    return time.strftime('%Y-%m-%d', time.gmtime(time.time() + 3 * 3600))


def free_left(acc):
    return max(0, FREE_DAILY - _used.get((_today(), acc['token']), 0))


def load_pool():
    global _pool, _pool_mtime
    try:
        mt = os.path.getmtime(BANK_PATH)
    except OSError:
        return
    if mt == _pool_mtime and _pool:
        return
    toks = set()
    rows = []
    try:
        with open(BANK_PATH, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get('token')
                if t and t not in toks:
                    toks.add(t)
                    rows.append({'token': t, 'balance': float(d.get('balance') or 0),
                                 'email': d.get('email') or t[:8],
                                 'dead': False, 'cooldown_until': 0.0,
                                 'dead_probes': 0, 'next_probe': 0.0})
    except OSError:
        return
    if rows:
        with _lock:
            old = {a['token']: a for a in _pool}
            for r in rows:
                o = old.get(r['token'])
                if o is not None:
                    # in-memory balance is fresher; keep quarantine state too
                    r['balance'] = o['balance']
                    for k in ('dead', 'cooldown_until', 'dead_probes', 'next_probe'):
                        r[k] = o[k]
            _pool = rows
        _pool_mtime = mt


def pick(kind='text', skip=(), need=0.0):
    """Auto-switcher: alive, off-cooldown accounts not tried yet.
    Text prefers accounts with free daily quota (free costs nothing regardless
    of balance); charged work only goes to accounts that can cover `need`."""
    load_pool()
    now = time.time()
    with _lock:
        cand = [a for a in _pool if not a['dead'] and a['cooldown_until'] <= now
                and a['token'] not in skip]
        busy = [a for a in cand if a['token'] in _busy]
        if busy and len(busy) < len(cand):
            cand = [a for a in cand if a['token'] not in _busy]
        if not cand:
            return None
        if kind == 'text':
            free = [a for a in cand if free_left(a) > 0]
            if free:
                cand = free
                need = 0.0
        if need > 0:
            cand = [a for a in cand if a['balance'] >= need]
            if not cand:
                return None
        if kind == 'text':
            cand.sort(key=lambda a: (_lat.get(a['token'], 10.0), -a['balance']))
            return cand[0]
        cand.sort(key=lambda a: -a['balance'])
        return random.choice(cand[:4])


def mark_dead(acc):
    with _lock:
        acc['dead'] = True
        acc['dead_probes'] = 0
        acc['next_probe'] = time.time()


def mark_cooldown(acc, secs):
    with _lock:
        acc['cooldown_until'] = time.time() + secs


def _recheck(acc):
    """Liveness/balance probe via /me/wheel (cheap GET). Returns True if the
    token is still valid; out-of-coins accounts revive once the daily wheel
    spin refills their balance."""
    req = urllib.request.Request(BASE + '/api/miniapp/me/wheel')
    req.add_header('Authorization', AU + acc['token'])
    req.add_header('User-Agent', UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
    except Exception:
        return False
    with _lock:
        acc['balance'] = float(d.get('balance') or acc['balance'])
    return True


def _reaper():
    """Housekeeping: revive dead accounts with exponential backoff (401/402/
    429 all retried, backing off to 1h), prune stale quota counters and old
    video tasks."""
    while True:
        time.sleep(REAP_INTERVAL)
        try:
            load_pool()
            now = time.time()
            today = _today()
            with _lock:
                todo = [a for a in _pool if a['dead'] and a['next_probe'] <= now]
                for k in [k for k in _used if k[0] != today]:
                    _used.pop(k, None)
                for tid in [t for t, v in _video_cache.items()
                            if now - v.get('created', now) > 86400]:
                    _video_cache.pop(tid, None)
            for acc in todo:
                ok = _recheck(acc)
                with _lock:
                    if ok:
                        acc['dead'] = False
                        acc['dead_probes'] = 0
                        acc['next_probe'] = 0.0
                        acc['cooldown_until'] = 0.0
                    else:
                        acc['dead_probes'] += 1
                        delay = min(300 * (2 ** acc['dead_probes']), 3600)
                        acc['next_probe'] = time.time() + delay
                if ok:
                    print('[pg_shim] revived %s balance=%.1f'
                          % (acc['email'], acc['balance']), flush=True)
                    event('revived %s (balance %.1f)' % (acc['email'], acc['balance']))
                else:
                    print('[pg_shim] probe failed %s, next in %ds'
                          % (acc['email'], int(acc['next_probe'] - time.time())), flush=True)
                time.sleep(1.5)
        except Exception:
            pass


def persist_balances():
    """Write live balances back into the bank (atomic replace). Only the
    balance field is touched; all other fields and unknown lines are kept."""
    global _pool_mtime
    if not os.path.exists(BANK_PATH):
        return
    with _lock:
        live = {a['token']: round(a['balance'], 2) for a in _pool}
    if not live:
        return
    try:
        with _bank_lock:
            with open(BANK_PATH, encoding='utf-8') as f:
                lines = f.readlines()
            out = []
            changed = False
            for line in lines:
                s = line.strip()
                d = None
                if s:
                    try:
                        d = json.loads(s)
                    except Exception:
                        d = None
                if isinstance(d, dict) and d.get('token') in live:
                    t = d['token']
                    if float(d.get('balance') or 0) != live[t]:
                        d['balance'] = live[t]
                        line = json.dumps(d, ensure_ascii=False) + '\n'
                        changed = True
                out.append(line if line.endswith('\n') else line + '\n')
            if not changed:
                return
            tmp = BANK_PATH + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.writelines(out)
            os.replace(tmp, BANK_PATH)
            with _lock:
                _pool_mtime = os.path.getmtime(BANK_PATH)
    except Exception:
        pass


def _persist_loop():
    while True:
        time.sleep(PERSIST_INTERVAL)
        persist_balances()


def _autofarm():
    """Auto-provision: when the pool's remaining free text quota drops below
    AUTOFARM_MIN_FREE, register AUTOFARM_BATCH fresh accounts via pg_farm
    (mail.tm, ~1 account/min). Backs off on mail.tm rate-limit failures."""
    try:
        import pg_farm
    except Exception as e:
        print('[pg_shim] autofarm disabled: %r' % (e,), flush=True)
        return
    pg_farm.BANK = BANK_PATH
    backoff = 0
    while True:
        time.sleep(max(AUTOFARM_INTERVAL, backoff))
        backoff = 0
        try:
            load_pool()
            with _lock:
                alive = [a for a in _pool if not a['dead']]
                left = sum(free_left(a) for a in alive)
            if left >= AUTOFARM_MIN_FREE:
                continue
            print('[pg_shim] autofarm: free_left=%d < %d, farming %d'
                  % (left, AUTOFARM_MIN_FREE, AUTOFARM_BATCH), flush=True)
            for _ in range(AUTOFARM_BATCH):
                with _bank_lock:
                    r = pg_farm.farm_one()
                if r.get('status') == 'ok':
                    print('[pg_shim] autofarm: +%s bal=%s'
                          % (r.get('email'), r.get('balance')), flush=True)
                    event('autofarm +%s (balance %s)' % (r.get('email'), r.get('balance')))
                else:
                    print('[pg_shim] autofarm fail: %s' % str(r)[:200], flush=True)
                    event('autofarm failed: %s' % str(r)[:80])
                    if r.get('err') == '429' or r.get('status') == 'mail_fail':
                        backoff = 600
                        break
                time.sleep(random.uniform(2, 5))
        except Exception as e:
            print('[pg_shim] autofarm error: %r' % (e,), flush=True)
            backoff = 120


def post_form(path, token, fields, timeout=240):
    b = '----pg' + uuid.uuid4().hex[:16]
    parts = []
    for k, v in fields.items():
        if v is None:
            continue
        parts.append('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v))
    body = (''.join(parts) + '--%s--\r\n' % b).encode('utf-8')
    req = urllib.request.Request(BASE + path, data=body, method='POST')
    req.add_header('Content-Type', 'multipart/form-data; boundary=%s' % b)
    req.add_header('Authorization', AU + token)
    req.add_header('User-Agent', UA)
    req.add_header('Origin', BASE)
    req.add_header('Referer', BASE + '/studio/')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def messages_to_prompt(messages, include_system=True, max_chars=None):
    out = []
    for m in messages or []:
        role = (m.get('role') or 'user').lower()
        content = m.get('content')
        if isinstance(content, list):
            content = ' '.join(c.get('text', '') for c in content if isinstance(c, dict))
        content = (content or '').strip()
        if not content:
            continue
        if role == 'system':
            if include_system:
                out.append('[System] ' + content)
        elif role == 'assistant':
            out.append('[Assistant] ' + content)
        else:
            out.append('[User] ' + content)
    dropped = False
    if max_chars:
        total = sum(len(x) + 2 for x in out)
        while len(out) > 1 and total > max_chars:
            total -= len(out[0]) + 2
            out.pop(0)
            dropped = True
        if out and total > max_chars:
            out[-1] = out[-1][-max_chars:]
            dropped = True
    text = '\n\n'.join(out)
    return ('[Earlier conversation omitted]\n\n' + text) if dropped else text


def moderation_hit(text):
    s = text.decode('utf-8', 'ignore') if isinstance(text, (bytes, bytearray)) else str(text)
    s = s.lower()
    return ('moderation' in s or 'запрещ' in s
            or 'prohibited content' in s or 'content policy' in s)


V8_FRAME = re.compile(r'^[ \t]+at .*?:\d+:\d+\)?[ \t]*$', re.M)


def sanitize_prompt(text):
    text = V8_FRAME.sub('', text)
    return re.sub(r'\n{3,}', '\n\n', text)


def dump_moderated_prompt(prompt):
    if os.environ.get('PG_DUMP_MODERATION') != '1':
        return
    try:
        with open('/tmp/opencode/pg_moderation_prompt.txt', 'w') as f:
            f.write(prompt[:2000000])
    except Exception:
        pass


def do_text(model, prompt, tries=6, fallbacks=None):
    variants = [prompt]
    for p in fallbacks or []:
        if p and p not in variants:
            variants.append(p)
    idx = 0
    skip = set()
    last_err = None
    for i in range(tries):
        acc = pick('text', skip, need=1.0)
        if acc is None:
            return 503, {'error': {'message': 'pg_shim: pool exhausted (%s)' % last_err,
                                   'type': 'upstream_unavailable'}}
        skip.add(acc['token'])
        _busy.add(acc['token'])
        fields = {
            'prompt': prompt, 'model': model,
            'response_format': 'text', 'response_length': 'medium', 'persona': 'universal',
            'provider_preference': '', 'context_enabled': 'false', 'context_messages_json': '[]',
            'showcase_link_token': '', 'showcase_link_item_id': '0',
            'serial_session_id': '', 'serial_operation_id': '', 'serial_series_index': '0',
            'studio_chat_id': '', 'client_request_id': uuid.uuid4().hex,
        }
        t0 = time.time()
        try:
            st, raw = post_form('/api/miniapp/text/generate', acc['token'], fields, timeout=90)
            d = json.loads(raw)
            if not d.get('ok'):
                last_err = str(d)[:200]
                if moderation_hit(last_err):
                    if idx == 0:
                        dump_moderated_prompt(prompt)
                    if idx + 1 < len(variants):
                        idx += 1
                        prompt = variants[idx]
                        print('[pg_shim] moderation hit: fallback %d/%d (prompt %d chars)'
                              % (idx, len(variants) - 1, len(prompt)), file=sys.stderr)
                        event('moderation: retry %d/%d without system/stack traces'
                              % (idx, len(variants) - 1))
                        continue
                    with _lock:
                        _stats['fail'] += 1
                    return 400, {'error': {'message': 'pg_shim: upstream moderation rejected the prompt (text_moderation_failed)',
                                           'type': 'moderation_error'}}
                continue
            with _lock:
                acc['balance'] = float(d.get('remaining_coins', acc['balance']))
                _stats['ok'] += 1
                if float(d.get('charged_cost') or 0) == 0:
                    _stats['free_hits'] += 1
                    k = (_today(), acc['token'])
                    _used[k] = _used.get(k, 0) + 1
                else:
                    _stats['coin_spend'] += float(d['charged_cost'])
                    # quota was spent upstream before our restart: don't
                    # keep preferring this account as "free" today
                    _used[(_today(), acc['token'])] = FREE_DAILY
                dt = time.time() - t0
                _lat[acc['token']] = 0.7 * _lat.get(acc['token'], 10.0) + 0.3 * dt
            print('[pg_shim] text ok %s in %.1fs (prompt %d chars)'
                  % (acc.get('email'), dt, len(prompt)), file=sys.stderr)
            event('text ok %s in %.1fs' % (acc.get('email'), dt))
            return 200, d
        except urllib.error.HTTPError as e:
            body = b''
            try:
                body = e.read()[:400]
            except Exception:
                pass
            last_err = '%s %s' % (e.code, body[:200])
            if e.code in (401, 403):
                mark_dead(acc)
            elif e.code == 429:
                mark_cooldown(acc, 120)
            elif e.code in (402, 400):
                low = body.lower()
                if moderation_hit(body):
                    if idx == 0:
                        dump_moderated_prompt(prompt)
                    if idx + 1 < len(variants):
                        idx += 1
                        prompt = variants[idx]
                        print('[pg_shim] moderation hit: fallback %d/%d (prompt %d chars)'
                              % (idx, len(variants) - 1, len(prompt)), file=sys.stderr)
                        event('moderation: retry %d/%d without system/stack traces'
                              % (idx, len(variants) - 1))
                        continue
                    with _lock:
                        _stats['fail'] += 1
                    return 400, {'error': {'message': 'pg_shim: upstream moderation rejected the prompt (text_moderation_failed)',
                                           'type': 'moderation_error'}}
                if b'coin' in low or b'\xd0\xb1\xd0\xb0\xd0\xbb' in low:
                    mark_dead(acc)
        except Exception as e:
            last_err = repr(e)[:200]
            slow = isinstance(e, (TimeoutError, urllib.error.URLError))
            mark_cooldown(acc, 600 if slow else 60)
        finally:
            _busy.discard(acc['token'])
    with _lock:
        _stats['fail'] += 1
    return 502, {'error': {'message': 'pg_shim: all retries failed: %s' % last_err,
                           'type': 'upstream_error'}}


def do_image(model, prompt, size, n):
    skip = set()
    last_err = None
    ratio = '1:1'
    if size:
        s = str(size).lower()
        if s.startswith(('1792', '1536', '16')) or 'x1024' in s and s.startswith(('17', '15', '16')):
            ratio = '16:9'
        elif 'x' in s:
            try:
                w, h = (int(x) for x in s.split('x')[:2])
                if w > h:
                    ratio = '16:9'
                elif h > w:
                    ratio = '9:16'
            except Exception:
                pass
    for i in range(5):
        acc = pick('image', skip, need=1.0)
        if acc is None:
            return 503, {'error': {'message': 'pg_shim: pool exhausted (%s)' % last_err}}
        skip.add(acc['token'])
        fields = {
            'prompt': prompt, 'model': model, 'aspect_ratio': ratio,
            'count': str(n), 'image_size': '2K', 'image_quality': 'high',
            'generation_mode': 'text_to_image',
            'persona': 'universal', 'provider_preference': '',
            'serial_session_id': '', 'serial_operation_id': '', 'serial_series_index': '0',
            'client_request_id': uuid.uuid4().hex, 'studio_chat_id': '',
            'context_enabled': 'false', 'context_messages_json': '[]',
            'showcase_link_token': '', 'showcase_link_item_id': '0',
        }
        try:
            st, raw = post_form('/api/miniapp/image/generate', acc['token'], fields, timeout=300)
            d = json.loads(raw)
            imgs = d.get('images') or d.get('durable_images') or []
            if not imgs:
                last_err = str(d)[:200]
                continue
            with _lock:
                acc['balance'] = float(d.get('remaining_coins', acc['balance']))
                _stats['ok'] += 1
                _stats['coin_spend'] += float(d.get('charged_cost') or 0)
            event('image %s x%d via %s' % (model, len(imgs), acc.get('email')))
            return 200, imgs, d
        except urllib.error.HTTPError as e:
            body = b''
            try:
                body = e.read()[:300]
            except Exception:
                pass
            last_err = '%s %s' % (e.code, body[:150])
            if e.code in (401, 403, 402):
                mark_dead(acc)
            elif e.code == 429:
                mark_cooldown(acc, 120)
        except Exception as e:
            last_err = repr(e)[:150]
            mark_cooldown(acc, 30)
    with _lock:
        _stats['fail'] += 1
    return 502, {'error': {'message': 'pg_shim image failed: %s' % last_err}}, None


def do_video(model, prompt, duration, ratio, quality):
    """Video generation: upstream returns video_url directly (2-4 min).
    Only accounts able to cover the job's coin cost are tried."""
    skip = set()
    last_err = None
    cost = video_cost(model, duration)
    audio = 'true'  # upstream rejects seedance-2-mini with generate_audio=false
    for i in range(4):
        acc = pick('video', skip, need=cost)
        if acc is None:
            with _lock:
                best = max([a['balance'] for a in _pool if not a['dead']] or [0])
            return 503, {'error': {'message': 'pg_shim: no account can afford %s '
                                               '(%.1f coins, top balance %.1f)'
                                               % (model, cost, best),
                                   'type': 'upstream_unavailable'}}
        skip.add(acc['token'])
        fields = {
            'prompt': prompt, 'model': model, 'mode': 'video', 'ratio': ratio or '16:9',
            'duration': str(duration or 5), 'generate_audio': audio,
            'submit_to_showcase': 'false', 'submit_title': '', 'author_nickname': '',
            'history_source': '', 'history_title': '', 'prompt_template_token': '',
            'showcase_link_token': '', 'showcase_link_item_id': '0',
            'serial_session_id': '', 'serial_operation_id': '', 'serial_series_index': '0',
            'studio_chat_id': '', 'client_request_id': uuid.uuid4().hex,
            'client_session_id': uuid.uuid4().hex,
        }
        if quality:
            fields['quality'] = quality
        try:
            st, raw = post_form('/api/miniapp/video/generate', acc['token'], fields, timeout=600)
            d = json.loads(raw)
            url = d.get('video_url') or d.get('durable_video_url')
            if not url:
                last_err = str(d)[:200]
                continue
            with _lock:
                acc['balance'] = float(d.get('remaining_coins', acc['balance']))
                _stats['ok'] += 1
                _stats['coin_spend'] += float(d.get('charged_cost') or 0)
            tid = d.get('task_id') or ('pg' + uuid.uuid4().hex[:16])
            res = {'task_id': tid, 'status': 'completed', 'video_url': url,
                   'charged_cost': d.get('charged_cost'), 'remaining_coins': d.get('remaining_coins')}
            return 200, res
        except urllib.error.HTTPError as e:
            body = b''
            try:
                body = e.read()[:300]
            except Exception:
                pass
            last_err = '%s %s' % (e.code, body[:150])
            if e.code in (401, 403, 402):
                mark_dead(acc)
            elif e.code == 429:
                mark_cooldown(acc, 120)
            elif e.code == 503:
                continue
        except Exception as e:
            last_err = repr(e)[:150]
            mark_cooldown(acc, 30)
    with _lock:
        _stats['fail'] += 1
    return 502, {'error': {'message': 'pg_shim video failed: %s' % last_err}}


def _video_worker(tid, model, prompt, duration, ratio, quality):
    try:
        st, res = do_video(model, prompt, duration, ratio, quality)
    except Exception as e:
        st, res = 502, {'error': {'message': repr(e)[:200]}}
    now = int(time.time())
    with _lock:
        if st == 200 and isinstance(res, dict) and res.get('video_url'):
            res = dict(res)
            res['created'] = now
            res['status'] = 'completed'
            upstream_id = res.get('task_id')
            res['task_id'] = tid
            _video_cache[tid] = res
            if upstream_id and upstream_id != tid:
                _video_cache[upstream_id] = res
        else:
            err = ''
            if isinstance(res, dict):
                err = (res.get('error') or {}).get('message') or ''
            _video_cache[tid] = {'task_id': tid, 'status': 'failed', 'created': now,
                                 'error': err or 'video generation failed'}
    if _video_cache[tid].get('status') == 'completed':
        event('video %s completed' % model)
    else:
        event('video %s failed: %s' % (model, _video_cache[tid].get('error', '')[:60]))


def _b64_image(url, timeout=60):
    req = urllib.request.Request(url)
    req.add_header('User-Agent', UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return base64.b64encode(r.read()).decode('ascii')


PANEL_HTML = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pg_shim // console</title>
<style>
:root{
  --bg:#070b08; --bg2:#0b110c; --panel:#0d140f; --line:#1e2c22; --line2:#2a3d2e;
  --txt:#c8d6c8; --dim:#63776a; --green:#5dff9a; --amber:#ffc857; --red:#ff6b6b; --cyan:#62d9ff;
}
*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0; background:radial-gradient(1200px 600px at 50% -10%, #0f1a12 0%, var(--bg) 60%);
  color:var(--txt); font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
  -webkit-font-smoothing:antialiased;
}
body::after{
  content:""; position:fixed; inset:0; pointer-events:none; z-index:9;
  background:repeating-linear-gradient(0deg, rgba(0,0,0,.22) 0 1px, transparent 1px 3px);
  opacity:.25;
}
header{
  display:flex; align-items:center; justify-content:space-between; gap:16px;
  padding:14px 22px; border-bottom:1px solid var(--line); background:rgba(9,14,10,.85);
  position:sticky; top:0; z-index:5; backdrop-filter:blur(6px);
}
.brand{font-size:15px; letter-spacing:.08em; color:var(--green); text-shadow:0 0 14px rgba(93,255,154,.25)}
.brand .dot{display:inline-block; width:8px; height:8px; background:var(--green); border-radius:50%;
  margin-right:8px; box-shadow:0 0 10px var(--green)}
.cursor{display:inline-block; width:7px; height:15px; background:var(--green); margin-left:6px;
  vertical-align:-2px; animation:blink 1.1s steps(1) infinite}
@keyframes blink{50%{opacity:0}}
.meta{display:flex; align-items:center; gap:18px; font-size:11px; letter-spacing:.12em; color:var(--dim)}
.meta .on{color:var(--green)}
.meta .off{color:var(--red)}
button{
  font:inherit; font-size:11px; letter-spacing:.1em; color:var(--txt); background:transparent;
  border:1px solid var(--line2); padding:4px 10px; cursor:pointer; text-transform:uppercase;
}
button:hover{color:var(--green); border-color:var(--green)}
main{padding:22px; max-width:1360px; margin:0 auto; min-height:calc(100vh - 55px);
  display:flex; flex-direction:column}
.stats{display:grid; grid-template-columns:repeat(5,1fr); gap:12px; margin-bottom:18px}
.card{
  border:1px solid var(--line); background:linear-gradient(180deg,rgba(20,30,23,.55),rgba(10,16,12,.6));
  padding:12px 14px; position:relative; overflow:hidden;
}
.card::before{content:""; position:absolute; inset:0 0 auto 0; height:1px;
  background:linear-gradient(90deg,var(--green),transparent 60%); opacity:.5}
.card label{display:block; font-size:10px; letter-spacing:.18em; color:var(--dim)}
.card b{display:block; font-size:26px; font-weight:500; color:var(--green); margin-top:4px;
  text-shadow:0 0 16px rgba(93,255,154,.18)}
.card small{display:block; font-size:10px; color:var(--dim); letter-spacing:.06em}
.grid{display:grid; grid-template-columns:minmax(0,1.55fr) minmax(0,1fr); gap:14px}
.panel{border:1px solid var(--line); background:var(--panel); min-width:0}
.panel h2{
  margin:0; padding:10px 14px; font-size:11px; font-weight:500; letter-spacing:.18em;
  color:var(--dim); border-bottom:1px solid var(--line); display:flex; justify-content:space-between; align-items:center;
}
.tag{color:var(--dim); font-size:10px; letter-spacing:.08em; border:1px solid var(--line2); padding:2px 7px}
table{width:100%; border-collapse:collapse; table-layout:fixed}
th{font-size:10px; letter-spacing:.14em; color:var(--dim); font-weight:400; text-align:left;
  padding:8px 14px; border-bottom:1px solid var(--line)}
td{padding:9px 14px; border-bottom:1px dashed rgba(30,44,34,.55); white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:rgba(93,255,154,.035)}
.num{text-align:right}
.mail{color:#9fd9b2}
.pill{font-size:10px; letter-spacing:.08em; padding:2px 7px; border:1px solid}
.pill.live{color:var(--green); border-color:rgba(93,255,154,.4); background:rgba(93,255,154,.06)}
.pill.cool{color:var(--amber); border-color:rgba(255,200,87,.4); background:rgba(255,200,87,.06)}
.pill.dead{color:var(--red); border-color:rgba(255,107,107,.4); background:rgba(255,107,107,.06)}
.log{padding:10px 14px; max-height:430px; overflow:auto; scrollbar-width:thin}
.ev{display:flex; gap:12px; padding:3px 0; font-size:12px; color:var(--txt)}
.ev .t{color:var(--dim); flex:0 0 auto}
.ev .m{overflow-wrap:anywhere}
.ev.ok .m{color:var(--green)}
.ev.warn .m{color:var(--amber)}
.ev.err .m{color:var(--red)}
.ev.info .m{color:var(--cyan)}
.ev.empty{color:var(--dim); font-style:italic}
.foot{display:flex; justify-content:space-between; gap:16px; flex-wrap:wrap;
  margin-top:auto; padding-top:16px; border-top:1px dashed var(--line);
  color:var(--dim); font-size:10px; letter-spacing:.12em}
.foot .live{color:var(--green)}
.modal{position:fixed; inset:0; z-index:20; display:flex; align-items:center; justify-content:center;
  background:rgba(3,6,4,.72); backdrop-filter:blur(3px)}
.modal.hidden{display:none}
.modal form{border:1px solid var(--line2); background:var(--panel); padding:18px; width:min(420px,90vw)}
.modal label{display:block; font-size:10px; letter-spacing:.18em; color:var(--dim); margin-bottom:8px}
.modal input{width:100%; padding:8px 10px; background:#070b08; border:1px solid var(--line2);
  color:var(--green); font:inherit; outline:none}
.modal input:focus{border-color:var(--green)}
.modal .row{display:flex; gap:8px; margin-top:12px; justify-content:flex-end}
@media (max-width:980px){.grid{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}}
@media (prefers-reduced-motion:reduce){.cursor{animation:none}}
</style>
</head>
<body>
<header>
  <div class="brand"><span class="dot"></span>pointgpt.shim<span class="cursor"></span></div>
  <div class="meta"><span id="link" class="off">LINK --</span><span id="clock">--:--:--</span><button id="keybtn">KEY</button></div>
</header>
<main>
  <section class="stats">
    <div class="card"><label>ALIVE</label><b id="s-alive">--</b><small id="s-accounts">/ -- accounts</small></div>
    <div class="card"><label>POOL COINS</label><b id="s-coins">--</b><small>available</small></div>
    <div class="card"><label>FREE LEFT</label><b id="s-free">--</b><small>text requests</small></div>
    <div class="card"><label>REQUESTS</label><b id="s-req">--</b><small id="s-ok">ok -- · fail --</small></div>
    <div class="card"><label>SPEND</label><b id="s-spend">--</b><small>coins</small></div>
  </section>
  <section class="grid">
    <div class="panel">
      <h2>ACCOUNTS <span id="af" class="tag">autofarm --</span></h2>
      <table>
        <thead><tr><th style="width:38%">ACCOUNT</th><th>STATE</th><th class="num">BALANCE</th><th class="num">FREE</th><th class="num">LAT</th><th class="num">PROBES</th></tr></thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
    <div class="panel">
      <h2>EVENTS <span id="uptime" class="tag">up --</span></h2>
      <div id="events" class="log"></div>
    </div>
  </section>
  <footer class="foot">
    <span><span class="live">●</span> shim ready</span>
    <span>GET /health · GET /api/panel · POST /v1/chat/completions</span>
    <span>refresh 3s</span>
  </footer>
</main>
<div id="keymodal" class="modal hidden">
  <form id="keyform">
    <label>PG_KEY</label>
    <input id="keyinput" type="password" placeholder="bearer key, empty = no auth" autocomplete="off">
    <div class="row"><button type="button" id="keyclear">CLEAR</button><button type="submit">SAVE</button></div>
  </form>
</div>
<script>
const $ = (s) => document.querySelector(s);
let key = localStorage.getItem('pg_key') || '';
let timer = null;
const pad = (n) => String(n).padStart(2, '0');
function clock(){
  const d = new Date();
  $('#clock').textContent = pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}
function hms(t){
  const d = new Date(t * 1000);
  return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}
function fmt1(n){ return (Math.round((n || 0) * 10) / 10).toFixed(1); }
function uptime(s){
  s = Math.max(0, Math.floor(s || 0));
  return Math.floor(s/3600) + 'h ' + pad(Math.floor(s%3600/60)) + 'm ' + pad(s%60) + 's';
}
function link(ok){
  const el = $('#link');
  el.textContent = ok ? 'LINK ONLINE' : 'LINK OFFLINE';
  el.className = ok ? 'on' : 'off';
}
function showKey(){ $('#keymodal').classList.remove('hidden'); $('#keyinput').focus(); }
function hideKey(){ $('#keymodal').classList.add('hidden'); }
async function load(){
  try{
    const r = await fetch('/api/panel', {headers: key ? {Authorization: 'Bearer ' + key} : {}, cache: 'no-store'});
    if(r.status === 401){ link(false); showKey(); return; }
    if(!r.ok) throw new Error(r.status);
    const d = await r.json();
    render(d); link(true);
  }catch(e){ link(false); }
}
function render(d){
  $('#s-alive').textContent = d.pool.alive;
  $('#s-accounts').textContent = '/ ' + d.pool.accounts + ' accounts';
  $('#s-coins').textContent = fmt1(d.pool.coins);
  $('#s-free').textContent = d.pool.free;
  $('#s-req').textContent = d.stats.requests;
  $('#s-ok').textContent = 'ok ' + d.stats.ok + ' · fail ' + d.stats.fail;
  $('#s-spend').textContent = fmt1(d.stats.coin_spend);
  $('#af').textContent = d.autofarm.enabled ? 'autofarm on · min ' + d.autofarm.min_free : 'autofarm off';
  $('#uptime').textContent = 'up ' + uptime(d.uptime);
  const rows = $('#rows');
  rows.replaceChildren();
  for(const a of d.accounts){
    const st = a.dead ? 'dead' : (a.cooldown > 0 ? 'cool' : 'live');
    const label = a.dead ? 'DEAD' : (a.cooldown > 0 ? 'COOL ' + a.cooldown + 's' : 'LIVE');
    const tr = document.createElement('tr');
    const cells = [a.email, '', fmt1(a.balance), a.free, a.latency ? fmt1(a.latency) + 's' : '—', a.probes];
    for(let i = 0; i < cells.length; i++){
      const td = document.createElement('td');
      if(i === 0) td.className = 'mail';
      if(i >= 2) td.className = 'num';
      if(i === 1){
        const pill = document.createElement('span');
        pill.className = 'pill ' + st; pill.textContent = label;
        td.appendChild(pill);
      }else{
        td.textContent = cells[i];
      }
      tr.appendChild(td);
    }
    rows.appendChild(tr);
  }
  if(!d.accounts.length){
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 6; td.className = 'ev empty'; td.textContent = 'bank is empty';
    tr.appendChild(td); rows.appendChild(tr);
  }
  const log = $('#events');
  log.replaceChildren();
  for(const e of d.events){
    const div = document.createElement('div');
    div.className = 'ev';
    const m = e.msg || '';
    if(m.startsWith('text ok') || m.startsWith('image') || m.startsWith('video') && m.includes('completed')) div.classList.add('ok');
    else if(m.startsWith('moderation') || m.startsWith('autofarm failed')) div.classList.add('warn');
    else if(m.includes('failed')) div.classList.add('err');
    else if(m.startsWith('autofarm') || m.startsWith('revived')) div.classList.add('info');
    const t = document.createElement('span'); t.className = 't'; t.textContent = hms(e.t);
    const x = document.createElement('span'); x.className = 'm'; x.textContent = m;
    div.append(t, x); log.appendChild(div);
  }
  if(!d.events.length){
    const div = document.createElement('div'); div.className = 'ev empty';
    div.textContent = 'no events yet — send a request';
    log.appendChild(div);
  }
}
$('#keyform').addEventListener('submit', (e) => {
  e.preventDefault();
  key = $('#keyinput').value.trim();
  if(key) localStorage.setItem('pg_key', key); else localStorage.removeItem('pg_key');
  hideKey(); load();
});
$('#keyclear').addEventListener('click', () => {
  key = ''; localStorage.removeItem('pg_key'); hideKey(); load();
});
$('#keybtn').addEventListener('click', () => showKey());
$('#keymodal').addEventListener('click', (e) => { if(e.target.id === 'keymodal') hideKey(); });
setInterval(clock, 1000); clock();
load();
timer = setInterval(() => { if(!document.hidden) load(); }, 3000);
</script>
</body>
</html>'''


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        sys.stderr.write('[pg_shim %s] %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), fmt % args))

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if not API_KEY:
            return True
        got = self.headers.get('Authorization') or ''
        if hmac.compare_digest(got, 'Bearer ' + API_KEY):
            return True
        self.close_connection = True
        self._send(401, {'error': {'message': 'invalid api key', 'type': 'authentication_error'}})
        return False

    def _sse(self, chunks_list, model):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        rid = 'chatcmpl-pg' + uuid.uuid4().hex[:20]

        def w(payload):
            b = payload.encode('utf-8')
            self.wfile.write(b'%x\r\n' % len(b) + b + b'\r\n')
            self.wfile.flush()

        w('data: ' + json.dumps({'id': rid, 'object': 'chat.completion.chunk', 'created': int(time.time()),
                                 'model': model, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]}) + '\n\n')
        for ch in chunks_list:
            w('data: ' + json.dumps({'id': rid, 'object': 'chat.completion.chunk', 'created': int(time.time()),
                                     'model': model, 'choices': [{'index': 0, 'delta': {'content': ch}, 'finish_reason': None}]}) + '\n\n')
        w('data: ' + json.dumps({'id': rid, 'object': 'chat.completion.chunk', 'created': int(time.time()),
                                 'model': model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}) + '\n\n')
        w('data: [DONE]\n\n')
        w('')

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            body = PANEL_HTML.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._authorized():
            return
        if self.path == '/api/panel':
            load_pool()
            now = time.time()
            with _lock:
                accounts = []
                for a in _pool:
                    accounts.append({
                        'email': a.get('email'),
                        'balance': round(a['balance'], 1),
                        'free': free_left(a),
                        'dead': bool(a['dead']),
                        'cooldown': max(0, int(a['cooldown_until'] - now)),
                        'latency': round(_lat.get(a['token'], 0.0), 1),
                        'probes': int(a.get('dead_probes') or 0),
                    })
                accounts.sort(key=lambda x: (x['dead'], -x['balance']))
                alive = sum(1 for a in _pool if not a['dead'])
                coins = round(sum(a['balance'] for a in _pool if not a['dead']), 1)
                free = sum(free_left(a) for a in _pool if not a['dead'])
                st = dict(_stats)
                pool_total = len(_pool)
            with _events_lock:
                events = list(_events)
            self._send(200, {
                'ok': True, 'now': now, 'uptime': int(now - STARTED),
                'pool': {'accounts': pool_total, 'alive': alive, 'coins': coins, 'free': free},
                'stats': st,
                'autofarm': {'enabled': AUTOFARM, 'min_free': AUTOFARM_MIN_FREE,
                             'batch': AUTOFARM_BATCH, 'interval': AUTOFARM_INTERVAL},
                'accounts': accounts, 'events': events,
            })
            return
        if self.path == '/health':
            load_pool()
            with _lock:
                alive = sum(1 for a in _pool if not a['dead'])
                coins = round(sum(a['balance'] for a in _pool if not a['dead']), 1)
                free = sum(free_left(a) for a in _pool if not a['dead'])
                st = dict(_stats)
            self._send(200, {'ok': True, 'alive_accounts': alive, 'total_accounts': len(_pool),
                             'pool_coins': coins, 'free_text_left': free, 'stats': st})
        elif self.path.startswith('/v1/models'):
            models = [{'id': m, 'object': 'model', 'created': 1761000000, 'owned_by': 'pointgpt'}
                      for m in IMAGE_MODELS + VIDEO_MODELS + TEXT_MODELS]
            self._send(200, {'object': 'list', 'data': models})
        elif self.path.startswith('/v1/video/tasks/'):
            tid = self.path.rsplit('/', 1)[-1].split('?')[0]
            with _lock:
                res = _video_cache.get(tid)
            if res is None:
                self._send(404, {'error': {'message': 'unknown task id'}})
            else:
                self._send(200, res)
        else:
            self._send(404, {'error': {'message': 'not found'}})

    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            req = json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            self._send(400, {'error': {'message': 'bad json'}})
            return
        if not self._authorized():
            return
        with _lock:
            _stats['requests'] += 1
        if self.path == '/v1/chat/completions':
            model = req.get('model') or 'gpt-6-astra'
            messages = req.get('messages')
            prompt = messages_to_prompt(messages)
            if not prompt:
                self._send(400, {'error': {'message': 'empty messages'}})
                return
            fallback = messages_to_prompt(messages, include_system=False)
            sanitized = sanitize_prompt(prompt)
            sanitized_nosys = sanitize_prompt(fallback)
            tail = messages_to_prompt(messages, include_system=False, max_chars=60000)
            st, d = do_text(model, prompt, fallbacks=[sanitized, fallback, sanitized_nosys, tail])
            if st != 200:
                self._send(st, d)
                return
            answer = d.get('answer') or ''
            if req.get('stream'):
                chunks = [answer[i:i + 24] for i in range(0, len(answer), 24)] or ['']
                try:
                    self._sse(chunks, model)
                except Exception:
                    pass
                return
            self._send(200, {
                'id': 'chatcmpl-pg' + uuid.uuid4().hex[:20], 'object': 'chat.completion',
                'created': int(time.time()), 'model': model,
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': answer},
                             'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': len(prompt) // 4, 'completion_tokens': len(answer) // 4,
                          'total_tokens': (len(prompt) + len(answer)) // 4},
                'pg_meta': {'charged_cost': d.get('charged_cost'), 'remaining_coins': d.get('remaining_coins')},
            })
        elif self.path.startswith('/v1/video/generations'):
            model = req.get('model') or 'minimax-h3'
            prompt = req.get('prompt') or ''
            duration = req.get('duration') or 5
            ratio = req.get('ratio') or req.get('size') or '16:9'
            quality = req.get('quality')
            if req.get('sync'):
                st, d = do_video(model, prompt, duration, ratio, quality)
                if st == 200 and isinstance(d, dict) and d.get('task_id'):
                    with _lock:
                        _video_cache.setdefault(d['task_id'], dict(d, created=int(time.time())))
                self._send(st, d)
                return
            tid = 'pg' + uuid.uuid4().hex[:16]
            with _lock:
                _video_cache[tid] = {'task_id': tid, 'status': 'processing',
                                     'model': model, 'created': int(time.time())}
            threading.Thread(target=_video_worker,
                             args=(tid, model, prompt, duration, ratio, quality),
                             daemon=True).start()
            self._send(200, {'task_id': tid, 'status': 'processing', 'model': model,
                             'poll': '/v1/video/tasks/' + tid})
        elif self.path == '/v1/images/generations':
            model = req.get('model') or 'nanobanano-2'
            if model in ('dall-e-3', 'dall-e-2', 'gpt-image-1'):
                model = 'nanobanano-2'
            prompt = req.get('prompt') or ''
            try:
                n = max(1, min(10, int(req.get('n') or 1)))
            except Exception:
                n = 1
            st, d, meta = do_image(model, prompt, req.get('size'), n)
            if st != 200:
                self._send(st, d)
                return
            fmt = req.get('response_format') or 'url'
            data = []
            for u in d[:n]:
                item = {'url': u}
                if fmt == 'b64_json':
                    try:
                        item = {'b64_json': _b64_image(u)}
                    except Exception:
                        pass
                data.append(item)
            self._send(200, {'created': int(time.time()), 'data': data})
        else:
            self._send(404, {'error': {'message': 'not found'}})


def main():
    load_pool()
    print('[pg_shim] bank=%s accounts=%d bind=%s:%d autofarm=%s'
          % (BANK_PATH, len(_pool), BIND_HOST, PORT, AUTOFARM), flush=True)
    print('[pg_shim] panel: http://%s:%d/' % (BIND_HOST, PORT), flush=True)
    threading.Thread(target=_reaper, daemon=True).start()
    threading.Thread(target=_persist_loop, daemon=True).start()
    if AUTOFARM:
        threading.Thread(target=_autofarm, daemon=True).start()
    atexit.register(persist_balances)
    srv = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == '__main__':
    main()
