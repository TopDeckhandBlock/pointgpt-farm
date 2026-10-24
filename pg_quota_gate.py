#!/usr/bin/env python3
"""pg_quota_gate.py - per-API-key daily quota in front of new-api for media gens.

Why: the PointGPT pool holds a finite number of coins per day (one wheel spin per
account per day). A single client with unlimited key quota drains it in ~2 hours.
This gate counts generation requests per client API key per UTC day and answers 429
after PG_DAILY_PER_KEY. GET (video polling) is never counted.

stdlib only, Python 3.9+.
"""
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get('PGQ_UPSTREAM', 'http://127.0.0.1:48001')
LISTEN_PORT = int(os.environ.get('PGQ_PORT', '8806'))
DAILY = int(os.environ.get('PG_DAILY_PER_KEY', '5'))
STATE = os.environ.get('PGQ_STATE', '/opt/pg-quota/quota.json')
UNLIM = set(k.strip() for k in os.environ.get('PG_UNLIMITED_KEYS', '').split(',') if k.strip())
GEN_PATHS = ('/v1/images/generations', '/v1/video/generations')

_lock = threading.Lock()
_state = {'date': '', 'counts': {}}


def _load():
    try:
        with open(STATE, encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict):
            _state['date'] = d.get('date') or ''
            _state['counts'] = d.get('counts') or {}
    except Exception:
        pass


def _save():
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_state, f, separators=(',', ':'))
        os.replace(tmp, STATE)
    except Exception as e:
        sys.stderr.write('quota save failed: %r\n' % (e,))


def _today():
    return time.strftime('%Y-%m-%d', time.gmtime())


def take(key):
    """Reserve one generation for this key. Returns (allowed, used, limit)."""
    if DAILY <= 0:
        return True, 0, 0
    if not key:
        return True, 0, DAILY           # no auth header: upstream will reject anyway
    if key in UNLIM or key.startswith('pg-unlim'):
        return True, 0, DAILY
    with _lock:
        t = _today()
        if _state['date'] != t:
            _state['date'] = t
            _state['counts'] = {}
        used = int(_state['counts'].get(key, 0))
        if used >= DAILY:
            return False, used, DAILY
        _state['counts'][key] = used + 1
        _save()
        return True, used + 1, DAILY


def peek(key):
    with _lock:
        if _state['date'] != _today():
            return 0
        return int(_state['counts'].get(key, 0))


def key_of(handler):
    a = handler.headers.get('Authorization') or ''
    if a.lower().startswith('bearer '):
        a = a[7:]
    return a.strip()


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'pg-quota/1.0'

    def log_message(self, format, *args):
        sys.stderr.write('%s %s\n' % (time.strftime('%H:%M:%S'), format % args))

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _proxy(self, method, count=False):
        n = 0
        if method == 'POST':
            try:
                n = int(self.headers.get('Content-Length') or 0)
            except Exception:
                n = 0
        body = self.rfile.read(n) if n else b''
        key = key_of(self)

        path = self.path
        if count and path in GEN_PATHS:
            ok, used, lim = take(key)
            if not ok:
                self._send(429, {'error': {
                    'message': 'daily media quota reached: %d/%d per key per day (UTC). '
                               'Resets 00:00 UTC.' % (used, lim),
                    'type': 'quota_exceeded', 'code': 'pg_daily_quota',
                    'used': used, 'limit': lim}})
                return
            rem = max(0, lim - used) if lim else 0
        else:
            rem = DAILY - peek(key) if DAILY > 0 else 0

        req = urllib.request.Request(UPSTREAM + path, data=body if method == 'POST' else None,
                                     method=method)
        for h in ('Authorization', 'Content-Type', 'Accept', 'OpenAI-Beta'):
            v = self.headers.get(h)
            if v:
                req.add_header(h, v)
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                data = r.read()
                code = r.status
                ctype = r.headers.get('Content-Type', 'application/json')
        except urllib.error.HTTPError as e:
            data = e.read()
            code = e.code
            ctype = e.headers.get('Content-Type', 'application/json')
        except Exception as e:
            self._send(502, {'error': {'message': 'quota gate upstream error: %r' % (e,)}})
            return
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-PG-Quota-Remaining', str(rem))
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def do_POST(self):
        self._proxy('POST', count=True)

    def do_GET(self):
        if self.path.startswith('/__quota'):
            key = key_of(self)
            self._send(200, {'date': _today(), 'limit': DAILY, 'used': peek(key)})
            return
        self._proxy('GET', count=False)

    def do_HEAD(self):
        self._proxy('HEAD', count=False)


def main():
    _load()
    srv = ThreadingHTTPServer(('127.0.0.1', LISTEN_PORT), H)
    sys.stderr.write('pg_quota_gate on 127.0.0.1:%d -> %s (limit %d/day/key)\n'
                     % (LISTEN_PORT, UPSTREAM, DAILY))
    srv.serve_forever()


if __name__ == '__main__':
    main()