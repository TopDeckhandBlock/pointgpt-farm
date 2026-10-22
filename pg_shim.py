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
import sys
import threading
import time
import uuid
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
_stats = {'requests': 0, 'ok': 0, 'fail': 0, 'free_hits': 0, 'coin_spend': 0.0}
FREE_DAILY = 5      # upstream: 5 free text requests / account / day (reset 21:00 UTC)


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
                else:
                    print('[pg_shim] autofarm fail: %s' % str(r)[:200], flush=True)
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


def messages_to_prompt(messages, include_system=True):
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
    return '\n\n'.join(out)


def moderation_hit(text):
    s = text.decode('utf-8', 'ignore') if isinstance(text, (bytes, bytearray)) else str(text)
    s = s.lower()
    return ('moderation' in s or 'запрещ' in s
            or 'prohibited content' in s or 'content policy' in s)


def do_text(model, prompt, tries=6, fallback_prompt=None):
    skip = set()
    last_err = None
    mod_hits = 0
    for i in range(tries):
        acc = pick('text', skip, need=1.0)
        if acc is None:
            return 503, {'error': {'message': 'pg_shim: pool exhausted (%s)' % last_err,
                                   'type': 'upstream_unavailable'}}
        skip.add(acc['token'])
        fields = {
            'prompt': prompt, 'model': model,
            'response_format': 'text', 'response_length': 'medium', 'persona': 'universal',
            'provider_preference': '', 'context_enabled': 'false', 'context_messages_json': '[]',
            'showcase_link_token': '', 'showcase_link_item_id': '0',
            'serial_session_id': '', 'serial_operation_id': '', 'serial_series_index': '0',
            'studio_chat_id': '', 'client_request_id': uuid.uuid4().hex,
        }
        try:
            st, raw = post_form('/api/miniapp/text/generate', acc['token'], fields)
            d = json.loads(raw)
            if not d.get('ok'):
                last_err = str(d)[:200]
                if moderation_hit(last_err):
                    mod_hits += 1
                    if mod_hits == 1 and fallback_prompt and fallback_prompt != prompt:
                        prompt = fallback_prompt
                        print('[pg_shim] moderation hit: retrying without system prompt',
                              file=sys.stderr)
                        continue
                    if mod_hits >= 2:
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
                    mod_hits += 1
                    if mod_hits == 1 and fallback_prompt and fallback_prompt != prompt:
                        prompt = fallback_prompt
                        print('[pg_shim] moderation hit: retrying without system prompt',
                              file=sys.stderr)
                        continue
                    if mod_hits >= 2:
                        with _lock:
                            _stats['fail'] += 1
                        return 400, {'error': {'message': 'pg_shim: upstream moderation rejected the prompt (text_moderation_failed)',
                                               'type': 'moderation_error'}}
                    continue
                if b'coin' in low or b'\xd0\xb1\xd0\xb0\xd0\xbb' in low:
                    mark_dead(acc)
        except Exception as e:
            last_err = repr(e)[:200]
            mark_cooldown(acc, 30)
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


def _b64_image(url, timeout=60):
    req = urllib.request.Request(url)
    req.add_header('User-Agent', UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return base64.b64encode(r.read()).decode('ascii')


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
        if not self._authorized():
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
            st, d = do_text(model, prompt, fallback_prompt=fallback or None)
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
