#!/usr/bin/env python3
"""pg_shim.py - OpenAI-compatible gateway over PointGPT.ai miniapp API.

Pool of autoreg accounts (master_bank.jsonl: {"token": "***", "balance": ...} per line).
Endpoints:
  GET  /v1/models
  POST /v1/chat/completions   (stream + non-stream; tools ignored, plain text upstream)
  POST /v1/images/generations (Nano Banana 2 / GPT Image 2.5 / Seedream 5 ...)
  POST /v1/video/generations  (minimax-h3 / grok / seedance-2-mini / wan-3-0 / veo; sync, ~3min)
  POST /v1/audio/generations  (poyo-sumo-v5 = Suno V5, 20 coins; sync, ~60s)
  GET  /health
Env: PG_BANK (path, default ./master_bank.jsonl), PG_PORT (default 8797).
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

MUSIC_MODELS = ['poyo-sumo-v5']
_music_cache = {}

# --- media rate limit: pool coins are burned by public traffic, cap media QPS ---
GEN_PATHS = ('/v1/images/generations', '/v1/video/generations', '/v1/audio/generations')
RATE_PER_MIN = int(os.environ.get('PG_RATE_PER_MIN', '20'))
_rl_hits = []
_rl_lock = threading.Lock()


def rate_ok():
    """Sliding-window limiter: at most RATE_PER_MIN media requests per 60s."""
    now = time.time()
    with _rl_lock:
        while _rl_hits and now - _rl_hits[0] > 60:
            _rl_hits.pop(0)
        if len(_rl_hits) >= RATE_PER_MIN:
            return False, int(60 - (now - _rl_hits[0])) + 1
        _rl_hits.append(now)
    return True, 0

_lock = threading.Lock()
_pool = []          # list of dicts: token, balance, dead, cooldown_until
_pool_mtime = 0.0
_used = {}          # (utcdate, token) -> free requests used today
_stats = {'requests': 0, 'ok': 0, 'fail': 0, 'free_hits': 0, 'coin_spend': 0.0}


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
            _pool = rows
        _pool_mtime = mt


def pick(skip=(), min_balance=0.0):
    """Round-robin over alive tokens, cheapest-cooldown first."""
    load_pool()
    now = time.time()
    with _lock:
        cand = [a for a in _pool if not a['dead'] and a['cooldown_until'] <= now
                and a['token'] not in skip and a['balance'] >= min_balance]
        if not cand and min_balance > 0:
            cand = [a for a in _pool if not a['dead'] and a['cooldown_until'] <= now
                    and a['token'] not in skip]
        if not cand:
            return None
        random.shuffle(cand)
        # prefer accounts with more coins so free-tier accounts survive
        cand.sort(key=lambda a: -a['balance'])
        top = cand[:8]
        return random.choice(top)


def mark_dead(acc):
    with _lock:
        acc['dead'] = True


def mark_cooldown(acc, secs):
    with _lock:
        acc['cooldown_until'] = time.time() + secs


SOCKS = os.environ.get('PG_SOCKS', '')
if SOCKS:
    import socks, sockshandler
    _sh, _sp = SOCKS.split(':')[0], int(SOCKS.split(':')[1])
    _OPENER = urllib.request.build_opener(sockshandler.SocksiPyHandler(socks.SOCKS5, _sh, _sp))
else:
    _OPENER = urllib.request.build_opener()


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
    with _OPENER.open(req, timeout=timeout) as r:
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
        acc = pick(skip)
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
        acc = pick(skip)
        if acc is None:
            return 503, {'error': {'message': 'pg_shim: pool exhausted (%s)' % last_err}}, None
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
            lowcoins = e.code == 402 and (b'\xd0\x9c\xd0\xbe\xd0\xbd\xd0\xb5\xd1\x82' in body or b'coin' in body.lower() or b'credits' in body.lower())
            if lowcoins:
                with _lock:
                    acc['balance'] = 0.0
                mark_cooldown(acc, 3600)
            elif e.code in (401, 403, 402):
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
    need = 12.0 if 'minimax' in model else 8.0
    for i in range(8):
        acc = pick(skip, need)
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
            lowcoins = e.code == 402 and (b'\xd0\x9c\xd0\xbe\xd0\xbd\xd0\xb5\xd1\x82' in body or b'coin' in body.lower() or b'credits' in body.lower())
            if lowcoins:
                with _lock:
                    acc['balance'] = 0.0
                mark_cooldown(acc, 3600)
            elif e.code in (401, 403, 402):
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


def do_music(model, prompt, title, style, with_lyrics, negative_tags, vocal_gender):
    """Sync music generation (Suno V5 = 20 coins, ~60s). Returns audio_url."""
    skip = set()
    last_err = None
    for i in range(8):
        acc = pick(skip, 20.0)
        if acc is None:
            return 503, {'error': {'message': 'pg_shim: pool exhausted (%s)' % last_err}}
        skip.add(acc['token'])
        fields = {
            'prompt': prompt, 'mode': 'simple', 'with_lyrics': 'true' if with_lyrics else 'false',
            'style': style or '', 'negative_tags': negative_tags or '',
            'vocal_gender': vocal_gender or '', 'style_weight': '', 'weirdness_constraint': '',
            'audio_weight': '', 'persona_id': '', 'title': (title or '')[:80],
            'model': model or 'poyo-sumo-v5', 'voice': '', 'stability': '', 'language_code': '',
            'apply_text_normalization': 'on', 'timestamps': 'false', 'emotion': '',
            'voice_style': '', 'dialogue': 'false', 'voice_secondary': '',
            'submit_to_showcase': 'false', 'submit_title': '', 'author_nickname': '',
            'showcase_link_token': '', 'showcase_link_item_id': '0',
            'studio_chat_id': '', 'client_request_id': uuid.uuid4().hex,
        }
        try:
            st, raw = post_form('/api/miniapp/audio/generate', acc['token'], fields, timeout=420)
            d = json.loads(raw)
            url = d.get('audio_url')
            if not url:
                last_err = str(d)[:200]
                continue
            with _lock:
                acc['balance'] = float(d.get('remaining_coins', acc['balance']))
                _stats['ok'] += 1
                _stats['coin_spend'] += float(d.get('charged_cost') or 0)
            tid = d.get('task_id') or ('pgm' + uuid.uuid4().hex[:16])
            res = {'task_id': tid, 'status': 'completed', 'audio_url': url,
                   'charged_cost': d.get('charged_cost'), 'remaining_coins': d.get('remaining_coins')}
            _music_cache[tid] = res
            return 200, res
        except urllib.error.HTTPError as e:
            body = b''
            try:
                body = e.read()[:300]
            except Exception:
                pass
            last_err = '%s %s' % (e.code, body[:150])
            lowcoins = e.code == 402 and (b'\xd0\x9c\xd0\xbe\xd0\xbd\xd0\xb5\xd1\x82' in body or b'coin' in body.lower() or b'credits' in body.lower())
            if lowcoins:
                with _lock:
                    acc['balance'] = 0.0
                mark_cooldown(acc, 3600)
            elif e.code in (401, 403, 402):
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
    return 502, {'error': {'message': 'pg_shim music failed: %s' % last_err}}


_video_jobs = {}  # [PGVID 2210] sora-style async jobs: id -> {status, url, ...}


def _video_worker(jid, model, prompt, duration, ratio, quality):
    _video_jobs[jid]['status'] = 'in_progress'
    st, res = do_video(model, prompt, duration, ratio, quality)
    if st == 200 and isinstance(res, dict) and res.get('video_url'):
        _video_jobs[jid].update({'status': 'completed', 'url': res['video_url'],
                                 'task_id': res.get('task_id'), 'charged_cost': res.get('charged_cost')})
    else:
        _video_jobs[jid].update({'status': 'failed', 'error': str(res)[:250]})


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        sys.stderr.write('[pg_shim] %s\n' % (fmt % args))

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
                st = dict(_stats)
            self._send(200, {'ok': True, 'alive_accounts': alive, 'total_accounts': len(_pool),
                             'pool_coins': coins, 'stats': st})
        elif self.path.startswith('/v1/videos/'):  # [PGVID 2210] sora-style poll
            parts = self.path.rstrip('/').split('/')
            vid = parts[3] if len(parts) > 3 else ''
            sub = parts[4] if len(parts) > 4 else ''
            j = _video_jobs.get(vid)
            if not j:
                self._send(404, {'error': {'message': 'unknown video id %s' % vid}})
                return
            if sub == 'content':
                if j.get('url'):
                    self.send_response(302)
                    self.send_header('Location', j['url'])
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                else:
                    self._send(404, {'error': {'message': 'video not ready'}})
                return
            out = {'id': vid, 'object': 'video', 'status': j['status'], 'model': j.get('model'),
                   'created': j.get('created')}
            if j['status'] == 'completed':
                out.update({'progress': 100, 'video_url': j['url'], 'download_uri': j['url'],
                            'url': j['url'], 'assets': [{'url': j['url']}],
                            'task_id': j.get('task_id'), 'charged_cost': j.get('charged_cost')})
            elif j['status'] == 'in_progress':
                out['progress'] = 50
            elif j['status'] == 'failed':
                out['error'] = j.get('error')
                out['progress'] = 100
            self._send(200, out)
        elif self.path.startswith('/v1/models'):
            models = [{'id': m, 'object': 'model', 'created': 1761000000, 'owned_by': 'pointgpt'}
                      for m in IMAGE_MODELS + VIDEO_MODELS + MUSIC_MODELS + TEXT_MODELS]
            self._send(200, {'object': 'list', 'data': models})
        else:
            self._send(404, {'error': {'message': 'not found'}})

    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            req = json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            self._send(400, {'error': {'message': 'bad json'}})
            return
        if self.path in GEN_PATHS:
            ok, wait = rate_ok()
            if not ok:
                self._send(429, {'error': {'message': 'pg rate limit: %d media requests/min, retry in %ds' % (RATE_PER_MIN, wait)}})
                return
        with _lock:
            _stats['requests'] += 1
        if self.path == '/v1/chat/completions':
            model = req.get('model') or 'gpt-6-astra'
            if model in MUSIC_MODELS or model == 'suno-v5':  # [PGMUS-CHAT 2210] Suno V5 через chat-маршрут (у форка new-api нет audio-relay на type=1)
                mprompt = messages_to_prompt(req.get('messages'))
                if not mprompt:
                    self._send(400, {'error': {'message': 'empty messages'}})
                    return
                st2, d2 = do_music('poyo-sumo-v5', mprompt, mprompt[:60], '', True, '', '')
                if st2 != 200:
                    self._send(st2, d2)
                    return
                answer = 'Track ready: ' + d2['audio_url']
                self._send(200, {'id': 'chatcmpl-pg' + uuid.uuid4().hex[:20], 'object': 'chat.completion',
                                 'created': int(time.time()), 'model': model,
                                 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': answer},
                                              'finish_reason': 'stop'}],
                                 'usage': {'prompt_tokens': len(mprompt) // 4, 'completion_tokens': 10,
                                           'total_tokens': len(mprompt) // 4 + 10},
                                 'pg_meta': {'audio_url': d2['audio_url'], 'charged_cost': d2.get('charged_cost')}})
                return
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
        elif self.path.startswith('/v1/audio/generations'):
            prompt = req.get('prompt') or req.get('input') or ''
            model = req.get('model') or 'poyo-sumo-v5'
            status, res = do_music(model, prompt, req.get('title') or '',
                                   req.get('style') or '',
                                   bool(req.get('with_lyrics', False)),
                                   req.get('negative_tags') or '',
                                   req.get('vocal_gender') or '')
            if status == 200:
                out = {'created': int(time.time()), 'data': [{'url': res['audio_url'], 'task_id': res['task_id']}],
                       'charged_cost': res.get('charged_cost')}
                self._send(200, out)
            else:
                self._send(status, res)
        elif self.path.startswith('/v1/audio/tasks/'):
            tid = self.path.rstrip('/').split('/')[-1]
            res = _music_cache.get(tid)
            self._send(200, res or {'task_id': tid, 'status': 'unknown'})
        elif self.path == '/v1/videos':  # [PGVID 2210] OpenAI Sora-style (new-api type=17 relay)
            model = req.get('model') or 'minimax-h3'
            prompt = req.get('prompt') or ''
            duration = req.get('duration') or req.get('seconds') or 5
            ratio = req.get('ratio') or ''
            size = req.get('size') or ''
            if not ratio and size and 'x' in str(size):
                try:
                    w, h = str(size).split('x')[:2]
                    ratio = '9:16' if int(h) > int(w) else '16:9'
                except Exception:
                    ratio = '16:9'
            ratio = ratio or '16:9'
            quality = str(req.get('quality') or '')
            jid = 'vid_' + uuid.uuid4().hex[:16]
            _video_jobs[jid] = {'status': 'queued', 'model': model, 'created': int(time.time()), 'progress': 0}
            threading.Thread(target=_video_worker, args=(jid, model, prompt, duration, ratio, quality), daemon=True).start()
            self._send(200, {'id': jid, 'object': 'video', 'status': 'queued', 'progress': 0,
                             'model': model, 'created': _video_jobs[jid]['created']})
        elif self.path.startswith('/v1/video/generations'):
            model = req.get('model') or 'minimax-h3'
            st, d = do_video(model, req.get('prompt') or '', req.get('duration') or 5,
                             req.get('ratio') or req.get('size') or '16:9', req.get('quality'))
            self._send(st, d)
        elif self.path.startswith('/v1/video/tasks/'):
            tid = self.path.rsplit('/', 1)[-1]
            res = _video_cache.get(tid)
            self._send(200, res or {'task_id': tid, 'status': 'unknown'})
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
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == '__main__':
    main()
