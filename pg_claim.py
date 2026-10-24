#!/usr/bin/env python3
"""pg_claim — claim all completed PointGPT achievements for every token in a bank.

POST /api/miniapp/me/achievements/<key>/claim credits coins and spins.
Runs sequentially with pacing; writes {token, coins_added, spins_added, balance} rows.
Stdlib only.
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "https://api.pointgpt.ai"


def call(method, path, tok, body=None, timeout=25):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + tok,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def main():
    src, out = sys.argv[1], sys.argv[2]
    pace = float(sys.argv[3]) if len(sys.argv) > 3 else 0.4
    tokens = []
    seen = set()
    for line in open(src, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            t = json.loads(line).get("token")
        except Exception:
            continue
        if t and t not in seen:
            seen.add(t)
            tokens.append(t)

    fh = open(out, "a", encoding="utf-8")
    tot_coins = tot_spins = 0
    for i, tok in enumerate(tokens, 1):
        st, ach = call("GET", "/api/miniapp/me/achievements", tok)
        if st != 200:
            time.sleep(pace)
            continue
        gained_c = gained_s = 0
        for item in ach.get("items") or []:
            key = item.get("key")
            if not key or not item.get("done") or item.get("claimed"):
                continue
            st2, r2 = call("POST", "/api/miniapp/me/achievements/%s/claim" % key, tok, {})
            if st2 == 200 and r2.get("credited"):
                gained_c += r2.get("coins_added") or 0
                gained_s += r2.get("spins_added") or 0
            time.sleep(0.2)
        st3, me = call("GET", "/api/miniapp/me", tok)
        bal = me.get("coins") or me.get("balance") or 0
        tot_coins += gained_c
        tot_spins += gained_s
        fh.write(json.dumps({"token": tok, "coins_added": gained_c,
                             "spins_added": gained_s, "balance": bal}) + "\n")
        fh.flush()
        if i % 20 == 0:
            print("progress %d/%d coins %s spins %s" % (i, len(tokens), tot_coins, tot_spins), flush=True)
        time.sleep(pace)
    print("DONE rows %d coins_added %d spins_added %d" % (len(tokens), tot_coins, tot_spins), flush=True)


if __name__ == "__main__":
    main()