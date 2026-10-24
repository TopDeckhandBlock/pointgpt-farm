#!/usr/bin/env python3
"""pg_audio_gate — public gate for PointGPT music (new-api has no /v1/audio/generations route).

Listens on 127.0.0.1:8805. Validates the caller's new-api key against
new-api /v1/models (cached 120s), then forwards the request to pg-shim
127.0.0.1:8799 unchanged. Stdlib only.
"""
import json
import os
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NEWAPI = "http://127.0.0.1:48001"
SHIM = "http://127.0.0.1:8799"
CACHE = {}
TTL = 120


def valid_key(auth):
    if not auth or not auth.startswith("Bearer "):
        return False, "missing bearer"
    key = auth[7:].strip()
    if not key:
        return False, "empty key"
    now = time.time()
    hit = CACHE.get(key)
    if hit and now - hit[1] < TTL:
        return hit[0], hit[2]
    ok, why = False, "rejected"
    try:
        req = urllib.request.Request(NEWAPI + "/v1/models", headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=12) as r:
            ok = r.status == 200
            why = "ok" if ok else "status %s" % r.status
    except urllib.error.HTTPError as e:
        why = "status %s" % e.code
    except Exception as e:
        why = "auth check failed: %s" % e
    CACHE[key] = (ok, now, why)
    if len(CACHE) > 2000:
        CACHE.clear()
    return ok, why


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        print("[gate] " + fmt % a, flush=True)

    def _send(self, code, body, ctype="application/json", close=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send(200, json.dumps({"ok": True, "cached": len(CACHE)}).encode())
        else:
            self._send(404, b'{"error":{"message":"not found"}}')

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        ok, why = valid_key(self.headers.get("Authorization", ""))
        if not ok:
            self._send(401, json.dumps({"error": {"message": "invalid api key: " + why}}).encode(), close=True)
            return
        path = self.path.split("?")[0]
        try:
            req = urllib.request.Request(SHIM + path, data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=300) as r:
                data = r.read()
                self._send(r.status, data, r.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as e:
            self._send(e.code, e.read())
        except Exception as e:
            self._send(502, json.dumps({"error": {"message": "shim failed: %s" % e}}).encode())


if __name__ == "__main__":
    port = int(os.environ.get("PG_AUDIO_PORT", "8805"))
    print("[gate] listening on 127.0.0.1:%d" % port, flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()