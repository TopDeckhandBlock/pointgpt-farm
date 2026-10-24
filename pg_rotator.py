#!/usr/bin/env python3
"""Rotate PointGPT email registration waves across VPS hosts.

Each host gets a small wave (default 5 accounts, 2 workers); hosts that answer
`Connection refused` are parked for COOLDOWN minutes. Local orchestrator, ssh based.

Usage: python3 pg_rotator.py [waves] [per_host]
Env: PG_COOLDOWN (min, default 40), PG_BANK_LOCAL (default pointgpt_email_bank.jsonl)
"""
import json, os, subprocess, sys, time

HOSTS = ["144.31.34.149", "13.143.162.135", "144.31.34.148", "144.31.34.150", "144.31.34.147"]
COOLDOWN = int(os.environ.get("PG_COOLDOWN", "40")) * 60
STATE = "pg_rotator_state.json"
BANK_LOCAL = os.environ.get("PG_BANK_LOCAL", "pointgpt_email_bank.jsonl")


def sh(cmd, timeout=900):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(s):
    tmp = STATE + ".tmp"
    json.dump(s, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE)


def wave(host, n, w):
    cmd = ("ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no root@%s "
           "\"cd /root && PG_BANK=/root/pg_email_bank.jsonl timeout %d python3 -u pg_emailfarm2.py %d %d /root/pg_mail.json\""
           % (host, n * 200 + 200, n, w))
    rc, out = sh(cmd, timeout=n * 200 + 300)
    ok = out.count("[OK]")
    refused = out.count("Connection refused") + out.count("timed out")
    return ok, refused, out


def pull(host):
    path = "pull_%s.jsonl" % host.replace(".", "_")
    cmd = ("scp -o ConnectTimeout=8 root@%s:/root/pg_email_bank.jsonl %s" % (host, path))
    sh(cmd, timeout=120)
    got = 0
    if os.path.exists(path):
        seen = set()
        if os.path.exists(BANK_LOCAL):
            for line in open(BANK_LOCAL, encoding="utf-8"):
                try:
                    seen.add(json.loads(line)["token"])
                except Exception:
                    pass
        with open(BANK_LOCAL, "a", encoding="utf-8") as f:
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("token") or d.get("auth_token")
                if not t or t in seen:
                    continue
                seen.add(t)
                f.write(json.dumps(d) + "\n")
                got += 1
    return got


def main():
    waves = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    per = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    st = load_state()
    for i in range(waves):
        for h in HOSTS:
            rec = st.get(h) or {}
            if rec.get("park_until", 0) > time.time():
                print("[SKIP-PARK]", h, int((rec["park_until"] - time.time()) / 60), "min", flush=True)
                continue
            ok, refused, out = wave(h, per, 2)
            added = pull(h)
            st[h] = {"last": int(time.time()), "ok_last": ok, "refused": refused}
            if refused >= per:
                st[h]["park_until"] = int(time.time() + COOLDOWN)
                print("[PARK]", h, "cooldown", COOLDOWN // 60, "min", flush=True)
            save_state(st)
            with open("pg_rotator.log", "a", encoding="utf-8") as f:
                f.write("== %s ok=%d refused=%d pulled=%d\n" % (h, ok, refused, added))
            print("[WAVE]", h, "ok", ok, "pulled", added, flush=True)
    total = sum(1 for _ in open(BANK_LOCAL, encoding="utf-8")) if os.path.exists(BANK_LOCAL) else 0
    print("DONE bank_local", total, flush=True)


if __name__ == "__main__":
    main()