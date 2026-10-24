#!/usr/bin/env python3
"""Refresh real coin balances for a PointGPT bank (stdlib only).

    python3 pg_refresh_balance.py in.jsonl out.jsonl [delay]

Reads token/auth_token from each row, GET /api/miniapp/me, writes
{token, balance, alive} keeping only rows that answered 200.
"""
import json, sys, time, urllib.error, urllib.request

API = "https://pointgpt.ai"
HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",
       "Origin": API, "Referer": API + "/"}

src = sys.argv[1] if len(sys.argv) > 1 else "master_bank.jsonl"
dst = sys.argv[2] if len(sys.argv) > 2 else "master_bank_fresh.jsonl"
delay = float(sys.argv[3]) if len(sys.argv) > 3 else 0.8

rows = [json.loads(l) for l in open(src, encoding="utf-8") if l.strip()]
out = open(dst, "w", encoding="utf-8")
alive = dead = 0
total = 0.0
for i, r in enumerate(rows, 1):
    tok = r.get("token") or r.get("auth_token")
    if not tok:
        continue
    req = urllib.request.Request(API + "/api/miniapp/me", headers=dict(HDR, Authorization="Bearer " + tok))
    try:
        x = urllib.request.urlopen(req, timeout=15)
        d = json.loads(x.read() or b"{}")
        bal = float(d.get("coins") or 0)
        alive += 1
        total += bal
        out.write(json.dumps({"token": tok, "balance": bal, "alive": True}) + "\n")
    except urllib.error.HTTPError as e:
        dead += 1
    except Exception:
        dead += 1
    if i % 10 == 0:
        out.flush()
        print("progress %d/%d alive %d sum %.1f" % (i, len(rows), alive, total), flush=True)
    time.sleep(delay)
out.close()
print("DONE rows %d alive %d dead %d pool %.1f" % (len(rows), alive, dead, total), flush=True)