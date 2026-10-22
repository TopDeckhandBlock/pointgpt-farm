#!/usr/bin/env python3
"""pg_shim.py - OpenAI-compatible gateway over PointGPT.ai miniapp API.

Pool of autoreg accounts (master_bank.jsonl: {"token": "***", "balance": ...} per line).
Endpoints:
  GET  /v1/models
  POST /v1/chat/completions   (stream + non-stream; tools ignored, plain text upstream)
  POST /v1/images/generations (Nano Banana 2 / GPT Image 2.5 / Seedream 5 ...)
  GET  /health
Env: PG_BANK (path, default ./master_bank.jsonl), PG_PORT (default 8797),
     PG_AUTOFARM (1/0, default 1), PG_AUTOFARM_MIN_FREE (default 5),
     PG_AUTOFARM_BATCH (default 1), PG_AUTOFARM_INTERVAL (sec, default 120).
Stdlib only. Python 3.9+.
"""
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
_video_cache = {}   # task_id -> result dict

_lock = threading.Lock()
_pool = []          # list of dicts: token, balance, dead, cooldown_until
_pool_mtime = 0.0
_used = {}          # (utcdate, token) -> free requests used today
_stats = {'requests': 0, 'ok': 0, 'fail': 0, 'free_hits': 0, 'coin_spend': 0.0}
FREE_DAILY = 5      # upstream: 5 free text requests / account / day (reset 21:00 UTC)


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
                                 'dead': False, 'cooldown_until': 0.0})
    except OSError:
        return
    if rows:
        with _lock:
            old = {a['token']: a for a in _pool}
            for r in rows:
                o = old.get(r['token'])
                if o is not None:
                    # keep quarantine state across bank reloads
                    r['dead'] = o['dead']
                    r['cooldown_until'] = o['cooldown_until']
            _pool = rows
        _pool_mtime = mt


def pick(kind='text', skip=()):
    """Auto-switcher: round-robin over alive accounts.
    Text requests prefer accounts with free daily quota left, then richest."""
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
        cand.sort(key=lambda a: -a['balance'])
        return random.choice(cand[:4])


def mark_dead(acc):
    with _lock:
        acc['dead'] = True


def mark_cooldown(acc, secs):
    with _lock:
        acc['cooldown_until'] = time.time() + secs


def _recheck(acc):
    """Liveness probe for a dead account via /me/wheel: 402-dead accounts
    (out of coins) can revive after the daily wheel spin refills the balance,
    so dead is not permanent."""
    req = urllib.request.Request(BASE + '/api/miniapp/me/wheel')
    req.add_header('Authorization', AU + acc['token'])
    req.add_header('User-Agent', UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        with _lock:
            acc['balance'] = float(d.get('balance') or acc['balance'])
            acc['dead'] = False
            acc['cooldown_until'] = 0.0
    except urllib.error.HTTPError as e:
        pass  # 401/403: token really dead, stays dead; 429/5xx: retry next round
    except Exception:
        pass


def _reaper():
    """Auto-switcher daemon: periodically probes dead accounts and revives
    the ones that came back (daily wheel refills coins at 00:00 MSK)."""
    while True:
        time.sleep(300)
        try:
            load_pool()
            with _lock:
                dead = [a for a in _pool if a['dead']]
            for acc in dead:
                _recheck(acc)
                time.sleep(1.5)
        except Exception:
            pass


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


def messages_to_prompt(messages):
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
            out.append('[System] ' + content)
        elif role == 'assistant':
            out.append('[Assistant] ' + content)
        else:
            out.append('[User] ' + content)
    return '\n\n'.join(out)


def do_text(model, prompt, tries=6):
    skip = set()
    last_err = None
    for i in range(tries):
        acc = pick('text', skip)
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
                if b'coin' in body.lower() or b'\xd0\xb1\xd0\xb0\xd0\xbb' in body.lower() or b'moderation' in body.lower():
                    if b'moderation' in body.lower():
                        continue  # prompt-level false positive: try another token once
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
        acc = pick('image', skip)
        if acc is None:
            return 503, {'error': {'message': 'pg_shim: pool exhausted (%s)' % last_err}}
        skip.add(acc['token'])
        fields = {
            'prompt': prompt, 'model': model, 'aspect_ratio': ratio,
            'image_size': '2K', 'image_quality': 'high', 'generation_mode': 'text_to_image',
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
    """Sync video generation: upstream returns video_url directly (2-4 min)."""
    skip = set()
    last_err = None
    audio = 'false' if model.startswith('seedance') else 'true'
    for i in range(4):
        acc = pick('video', skip)
        if acc is None:
            return 503, {'error': {'message': 'pg_shim: pool exhausted (%s)' % last_err}}
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
            _video_cache[tid] = res
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
            tid = self.path.rsplit('/', 1)[-1]
            res = _video_cache.get(tid)
            self._send(200, res or {'task_id': tid, 'status': 'unknown'})
        else:
            self._send(404, {'error': {'message': 'not found'}})

    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            req = json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            self._send(400, {'error': {'message': 'bad json'}})
            return
        with _lock:
            _stats['requests'] += 1
        if self.path == '/v1/chat/completions':
            model = req.get('model') or 'gpt-6-astra'
            prompt = messages_to_prompt(req.get('messages'))
            if not prompt:
                self._send(400, {'error': {'message': 'empty messages'}})
                return
            st, d = do_text(model, prompt)
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
            st, d = do_video(model, req.get('prompt') or '', req.get('duration') or 5,
                             req.get('ratio') or req.get('size') or '16:9', req.get('quality'))
            self._send(st, d)
        elif self.path == '/v1/images/generations':
            model = req.get('model') or 'nanobanano-2'
            if model in ('dall-e-3', 'dall-e-2', 'gpt-image-1'):
                model = 'nanobanano-2'
            prompt = req.get('prompt') or ''
            st, d, meta = do_image(model, prompt, req.get('size'), req.get('n') or 1)
            if st != 200:
                self._send(st, d)
                return
            self._send(200, {'created': int(time.time()),
                             'data': [{'url': u} for u in d[:int(req.get('n') or 1) or 1]]})
        else:
            self._send(404, {'error': {'message': 'not found'}})


def main():
    load_pool()
    print('[pg_shim] bank=%s accounts=%d port=%d' % (BANK_PATH, len(_pool), PORT))
    threading.Thread(target=_reaper, daemon=True).start()
    if AUTOFARM:
        threading.Thread(target=_autofarm, daemon=True).start()
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == '__main__':
    main()
