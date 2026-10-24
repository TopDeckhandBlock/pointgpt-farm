#!/usr/bin/env python3
"""Watch whether PointGPT email signup is open again.

PointGPT closed email registration: POST /api/site-auth/email/request-code
returns 404 "Этот email не найден. Сначала привяжите его..." for any fresh
address (only TG / VK / Yandex signup is left).

The watcher probes request-code with a throwaway alias and treats anything
other than that 404 as "door open". State is kept so the Telegram alert fires
once per transition, not every run.

    python3 pg_email_watch.py [state.json]
"""
import json, os, sys, time, urllib.error, urllib.request

API = "https://pointgpt.ai"
STATE = sys.argv[1] if len(sys.argv) > 1 else "/root/pg_email_watch.json"
HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Origin": API,
    "Referer": API + "/",
    "Content-Type": "application/json",
}

TG_TOKEN_FILE = "/root/pg_tg_bot.txt"
TG_CHAT = "7448683285"


def probe():
    addr = "pgwatch%d@baradok609.gmail.com" % int(time.time())
    body = json.dumps({"email": addr}).encode()
    req = urllib.request.Request(API + "/api/site-auth/email/request-code", data=body, headers=HDR)
    try:
        r = urllib.request.urlopen(req, timeout=25)
        return "open", "%s %s" % (r.status, (r.read() or b"")[:160].decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        txt = (e.read() or b"")[:200].decode("utf-8", "replace")
        if e.code == 404 and "не найден" in txt:
            return "closed", "404 email_not_found"
        return "open", "%s %s" % (e.code, txt)
    except Exception as ex:
        return "error", type(ex).__name__


def notify(msg):
    try:
        tok = open(TG_TOKEN_FILE).read().strip()
    except Exception:
        return
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": msg}).encode()
    try:
        urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % tok, data=data, timeout=15)
    except Exception:
        pass


import urllib.parse  # noqa: E402  (kept after helpers for readability)

state = {}
if os.path.exists(STATE):
    try:
        state = json.load(open(STATE))
    except Exception:
        state = {}

status, detail = probe()
prev = state.get("status")
state.update({"status": status, "detail": detail, "ts": int(time.time())})
json.dump(state, open(STATE, "w"))
print("[pg-email-watch] %s %s (prev=%s)" % (status, detail, prev))

if status == "open" and prev != "open":
    notify("PointGPT: email-регистрация ОТКРЫЛАСЬ (%s). Запускай pg_emailfarm2.py" % detail)
elif status == "closed" and prev == "open":
    notify("PointGPT: email-регистрация снова закрыта (%s)" % detail)