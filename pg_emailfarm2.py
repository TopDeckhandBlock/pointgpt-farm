#!/usr/bin/env python3
"""PointGPT email autoreg v2: one shared IMAP reader thread + N HTTP workers.

Gmail plus-alias signup (no captcha). Bank line: {email, token, auth_token, coins, ts}
Env: PG_BANK, PG_TAG, PG_ALIAS_TTL
Usage: python3 pg_emailfarm2.py <count> <workers> [mail_config.json]
"""
import email, imaplib, json, os, random, re, string, sys, threading, time
import urllib.error, urllib.request, uuid

BASE = "https://pointgpt.ai"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
CODES = {}
CODES_LOCK = threading.Lock()
STOP = threading.Event()


def load_mail_cfg(path=None):
    for p in [path, os.environ.get("PG_MAIL_JSON"), "pg_mail.json",
              os.path.expanduser("~/tmp/logicc_autoreg/config.json")]:
        if not p or not os.path.exists(p):
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        c = (d.get("mail") or {}).get("catchall") or {}
        u = c.get("gmail_user") or d.get("gmail_user")
        pw = c.get("gmail_app_pass") or d.get("gmail_app_pass")
        if u and pw:
            return u, pw
    raise SystemExit("no mail creds")


def parse_msg(raw):
    m = email.message_from_bytes(raw)
    to = (m.get("To") or "").lower()
    body = ""
    if m.is_multipart():
        for part in m.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    body += part.get_payload(decode=True).decode("utf-8", "replace")
                except Exception:
                    pass
    else:
        try:
            body = m.get_payload(decode=True).decode("utf-8", "replace")
        except Exception:
            pass
    codes = re.findall(r"(?<!\d)(\d{6})(?!\d)", body)
    return to, (codes[0] if codes else None)


def reader(user, pw, prefix):
    seen_uids = set()
    fails = 0
    while not STOP.is_set():
        try:
            M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            M.login(user, pw)
            M.select("INBOX")
            typ, data = M.search(None, '(FROM "pointgpt" SINCE "%s")' % time.strftime("%d-%b-%Y"))
            ids = (data[0].split() if data and data[0] else [])[-80:]
            for i in ids:
                if i in seen_uids:
                    continue
                typ, msg = M.fetch(i, "(RFC822)")
                if typ != "OK" or not msg or not msg[0]:
                    continue
                to, code = parse_msg(msg[0][1])
                seen_uids.add(i)
                if not code:
                    seen_uids.add(i)
                    continue
                m = re.search(r"\+(" + re.escape(prefix) + r"[a-z0-9]{6,})@", to)
                if m:
                    with CODES_LOCK:
                        CODES[m.group(1)] = code
            M.logout()
            fails = 0
        except Exception as e:
            fails += 1
            if fails % 5 == 1:
                print("[IMAP-ERR]", repr(e)[:80], flush=True)
            if fails > 12:
                time.sleep(30)
        time.sleep(7)
    STOP.set()


def form(path, fields, token=None, timeout=90):
    b = "-" * 18 + uuid.uuid4().hex
    out = b""
    for k, v in fields.items():
        out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (b, k, v)).encode()
    out += ("--%s--\r\n" % b).encode()
    req = urllib.request.Request(BASE + path, data=out, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + b)
    req.add_header("User-Agent", UA)
    req.add_header("Origin", BASE)
    req.add_header("Referer", BASE + "/")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:220]
    except Exception as e:
        return 0, repr(e)


IMAP_LOCK = threading.Lock()


def wait_code(alias, timeout=150, user=None, pw=None):
    """Poll Gmail directly for the alias (single shared IMAP connection at a time)."""
    end = time.time() + timeout
    while time.time() < end:
        with CODES_LOCK:
            c = CODES.pop(alias.split("@")[0].split("+")[-1], None)
        if c:
            return c
        if user and pw:
            got = _poll_alias(user, pw, alias)
            if got:
                return got
        time.sleep(4)
    return None


def _poll_alias(user, pw, alias):
    if not IMAP_LOCK.acquire(timeout=60):
        return None
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        M.login(user, pw)
        M.select("INBOX")
        typ, data = M.search(None, '(TO "%s")' % alias)
        ids = data[0].split() if data and data[0] else []
        if ids:
            typ, msg = M.fetch(ids[-1], "(RFC822)")
            M.logout()
            if typ == "OK" and msg and msg[0]:
                _to, code = parse_msg(msg[0][1])
                if code:
                    return code
            return None
        M.logout()
    except Exception as e:
        print("[IMAP-ERR]", repr(e)[:70], flush=True)
    finally:
        try:
            IMAP_LOCK.release()
        except Exception:
            pass
    return None


def one(idx, user, prefix, bank, pw):
    tag = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))
    alias = "%s+%s%s@gmail.com" % (user.split("@")[0], prefix, tag)
    key = "%s%s" % (prefix, tag)
    st, body = form("/api/site-auth/email/request-code",
                    {"email": alias, "create_user_if_missing": "true"})
    if st != 200:
        print("[REQ-FAIL]", idx, st, body[:90].replace("\n", " "), flush=True)
        return False
    code = wait_code(key, 170, user, pw)
    if not code:
        print("[NOCODE]", idx, key, flush=True)
        return False
    st2, body2 = form("/api/site-auth/email/verify",
                      {"email": alias, "code": code, "idempotency_key": str(uuid.uuid4())})
    if st2 != 200:
        print("[VERIFY-FAIL]", idx, st2, body2[:90].replace("\n", " "), flush=True)
        return False
    try:
        d = json.loads(body2)
    except Exception:
        print("[BADJSON]", idx, body2[:80], flush=True)
        return False
    tok = d.get("auth_token")
    if not tok:
        print("[NOTOKEN]", idx, body2[:90].replace("\n", " "), flush=True)
        return False
    rec = {"email": alias, "token": tok, "auth_token": tok, "coins": None, "ts": int(time.time())}
    with CODES_LOCK:
        with open(bank, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    print("[OK]", idx, alias, "new", d.get("is_new_user"), flush=True)
    return True


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    w = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    user, pw = load_mail_cfg(sys.argv[3] if len(sys.argv) > 3 else None)
    prefix = os.environ.get("PG_TAG", "pg")
    bank = os.environ.get("PG_BANK", "pointgpt_email_bank.jsonl")
    # direct per-alias polling (see wait_code) - no shared reader thread needed
    q = list(range(n))
    ok = [0]
    lock = threading.Lock()

    def worker():
        while True:
            with lock:
                if not q:
                    return
                i = q.pop(0)
            try:
                if one(i, user, prefix, bank, pw):
                    with lock:
                        ok[0] += 1
            except Exception as e:
                print("[ERR]", i, repr(e)[:80], flush=True)

    ths = [threading.Thread(target=worker) for _ in range(w)]
    for x in ths:
        x.start()
    for x in ths:
        x.join()
    STOP.set()
    print("DONE ok=%d/%d" % (ok[0], n), flush=True)


if __name__ == "__main__":
    main()