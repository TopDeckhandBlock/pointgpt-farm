"""pg_tgfarm.py - PointGPT account farm via Telegram login (no email needed).

Flow per Telegram session:
  POST /api/site-auth/telegram/start            -> challenge_id + poll_token + login_url
  bot @GPT_onlinebot  /start web_login_<ch>     -> bot confirms login
  GET  /api/site-auth/telegram/status?...       -> {status: completed, auth_token}
  GET  /api/miniapp/me                          -> profile / coins
  POST /api/miniapp/me/wheel/spin                -> +2..15 coins
  append {token, balance} to bank jsonl

Env:
  PG_SESS   dir with .session files (default ./tgsess)
  PG_BANK   output bank (default ./pointgpt_bank.jsonl)
  PG_STATE  state json (default ./pg_tg_state.json)
  PG_LIMIT  max sessions to process this run (default all)
  PG_WORK   parallel workers (default 6)
"""
import asyncio, json, os, sys, glob, time, threading, urllib.request, urllib.error, http.cookiejar, urllib.parse

BASE = "https://pointgpt.ai"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

HERE = os.path.dirname(os.path.abspath(__file__))
SESS = os.environ.get("PG_SESS", os.path.join(HERE, "tgsess"))
BANK = os.environ.get("PG_BANK", os.path.join(HERE, "pointgpt_bank.jsonl"))
STATE = os.environ.get("PG_STATE", os.path.join(HERE, "pg_tg_state.json"))
LIMIT = int(os.environ.get("PG_LIMIT", "0")) or None
WORK = int(os.environ.get("PG_WORK", "6"))
BOT = "GPT_onlinebot"

state = {}


def load_state():
    global state
    try:
        state = json.load(open(STATE, encoding="utf-8"))
    except Exception:
        state = {}


_state_lock = threading.Lock()


def put_state(base, rec):
    """Записать статус, но никогда не затирать успешный 'ok' неудачей."""
    with _state_lock:
        cur = state.get(base) or {}
        if cur.get("status") == "ok" and rec.get("status") != "ok":
            return
        state[base] = rec


def save_state():
    with _state_lock:
        tmp = STATE + ".tmp.%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE)


SOCKS = os.environ.get("PG_SOCKS", "")   # "1" or host:port -> route HTTP through VPS
_handlers = []


class Api:
    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.ports = []
        if SOCKS:
            self.ports = ["127.0.0.1:1080"] if SOCKS == "1" else [x.strip() for x in SOCKS.split(",") if x.strip()]
        self._ops = {}
        self._rr = 0

    def _opener(self, hp=None):
        if hp is None:
            return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        if hp in self._ops:
            return self._ops[hp]
        import socks, sockshandler
        host, port = hp.split(":")[0], int(hp.split(":")[1])
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj),
                                        sockshandler.SocksiPyHandler(socks.SOCKS5, host, port))
        self._ops[hp] = op
        return op

    def _req(self, path, body=None, token=None, method=None, timeout=40):
        h = {"User-Agent": UA, "Accept": "application/json", "Origin": BASE,
             "Referer": BASE + "/studio/", "Cache-Control": "no-store"}
        d = None
        if body is not None:
            d = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        if token:
            h["Authorization"] = "Bearer " + token
        tries = self.ports or [None]
        last = None
        for k in range(len(tries) * 2):
            hp = tries[(self._rr + k) % len(tries)] if self.ports else None
            r = urllib.request.Request(BASE + path, data=d, headers=h, method=method)
            try:
                x = self._opener(hp).open(r, timeout=timeout)
                self._rr = (self._rr + k + 1) % max(1, len(tries))
                return x.status, x.read().decode("utf-8", "ignore")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "ignore")
            except Exception as e:
                last = repr(e)[:120]
        return 0, last or "no-route"

    def start(self):
        s, b = self._req("/api/site-auth/telegram/start", {}, method="POST")
        if s != 200:
            return None
        try:
            return json.loads(b)
        except Exception:
            return None

    def status(self, ch, pt):
        q = "/api/site-auth/telegram/status?" + urllib.parse.urlencode({"challenge_id": ch, "poll_token": pt})
        s, b = self._req(q)
        if s != 200:
            return None
        try:
            return json.loads(b)
        except Exception:
            return None

    def me(self, token):
        s, b = self._req("/api/miniapp/me", token=token)
        if s != 200:
            return None
        try:
            return json.loads(b)
        except Exception:
            return None

    def spin(self, token):
        s, b = self._req("/api/miniapp/me/wheel/spin", {}, token=token, method="POST", timeout=60)
        if s != 200:
            return None
        try:
            return json.loads(b)
        except Exception:
            return None


def bank_add(rec):
    with open(BANK, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


async def one(TelegramClient, path, sem, stats):
    base = os.path.splitext(os.path.basename(path))[0]
    st = state.get(base) or {}
    if st.get("status") in ("ok", "dead", "noauth", "nochallenge"):
        return
    async with sem:
        api = Api()
        try:
            cli = TelegramClient(path, API_ID, API_HASH)
        except Exception as e:
            put_state(base, {'status': 'dead', 'err': type(e).__name__, 'ts': int(time.time())})
            stats['dead'] += 1
            save_state()
            return
        try:
            try:
                await cli.connect()
            except Exception as e:
                put_state(base, {"status": "dead", "err": type(e).__name__, "ts": int(time.time())})
                stats["dead"] += 1
                save_state()
                return
            if not await cli.is_user_authorized():
                put_state(base, {"status": "noauth", "ts": int(time.time())})
                stats["noauth"] += 1
                save_state()
                return
            d = api.start()
            if not d or "challenge_id" not in d:
                put_state(base, {"status": "nochallenge", "ts": int(time.time())})
                stats["nochallenge"] += 1
                save_state()
                return
            ch, pt = d["challenge_id"], d["poll_token"]
            await cli.send_message(BOT, "/start " + d["login_url"].split("start=")[1])
            tok = None
            for i in range(8):
                await asyncio.sleep(3)
                r = api.status(ch, pt)
                if r and r.get("status") == "completed" and r.get("auth_token"):
                    tok = r["auth_token"]
                    break
            if not tok:
                put_state(base, {"status": "notoken", "ts": int(time.time())})
                stats["notoken"] += 1
                save_state()
                return
            me = api.me(tok) or {}
            sp = api.spin(tok) or {}
            bal = sp.get("balance_after")
            if bal is None:
                bal = me.get("coins")
            rec = {"status": "ok", "email": "tg:" + base, "provider": "telegram",
                   "tg_id": me.get("user_id"), "name": (me.get("full_name") or "")[:40],
                   "token": tok, "balance": float(bal or 0), "host": "local-tg",
                   "ts": int(time.time())}
            bank_add(rec)
            put_state(base, {"status": "ok", "token": tok, "balance": rec["balance"],
                           "ts": int(time.time())})
            stats["ok"] += 1
            save_state()
            print("[OK]", base, "bal", rec["balance"], "coins", sp.get("coins_added"), flush=True)
        finally:
            try:
                await cli.disconnect()
            except Exception:
                pass


async def main():
    from telethon import TelegramClient
    load_state()
    files = sorted(glob.glob(os.path.join(SESS, "*.session")))
    todo = [p for p in files if (state.get(os.path.splitext(os.path.basename(p))[0]) or {}).get("status")
            not in ("ok", "dead", "noauth", "nochallenge")]
    if LIMIT:
        todo = todo[:LIMIT]
    print("sessions:", len(files), "todo:", len(todo), "workers:", WORK, flush=True)
    sem = asyncio.Semaphore(WORK)
    stats = {"ok": 0, "dead": 0, "noauth": 0, "notoken": 0, "nochallenge": 0}
    res = await asyncio.gather(*[one(TelegramClient, p, sem, stats) for p in todo], return_exceptions=True)
    errs = [r for r in res if isinstance(r, BaseException)]
    if errs:
        print('worker errors:', len(errs), 'first:', type(errs[0]).__name__, flush=True)
    print("DONE", json.dumps(stats), "bank_lines", sum(1 for _ in open(BANK, encoding="utf-8")) if os.path.exists(BANK) else 0)


if __name__ == "__main__":
    asyncio.run(main())