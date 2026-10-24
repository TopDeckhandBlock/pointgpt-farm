# PointGPT.ai farm v2 — register via mail.tm OTP, spin wheel, bank tokens
# usage: PG_BANK=/path/bank.jsonl python3 pg_farm2.py N
# env: PG_BANK (default ./pointgpt_bank.jsonl), PG_DOMAIN (default maxxspace.com), PG_HOST (tag)
import urllib.request, urllib.error, json, random, string, time, re, sys, os

BANK = os.environ.get('PG_BANK', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pointgpt_bank.jsonl'))
DOMAIN = os.environ.get('PG_DOMAIN', 'maxxspace.com')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36'

ADJ = ['swift','brave','calm','dark','keen','lucky','misty','noble','pale','quick','red','silent','solar','tiny','vivid','wild','azure','bold','cosmic','dusty','echo','frost','golden','hollow','iron','jolly','lunar','mellow','neon','onyx']
NOUN = ['fox','wolf','bear','hawk','lynx','orca','raven','tiger','viper','whale','comet','delta','ember','flint','grove','harbor','iris','jade','karma','lotus','metro','nova','onyx','pixel','quartz','river','sage','trail','urban','vector']

def human_user():
    style = random.randint(0, 3)
    a, n = random.choice(ADJ), random.choice(NOUN)
    if style == 0:
        return f'{a}.{n}{random.randint(10, 999)}'
    if style == 1:
        return f'{a}{n}_{random.randint(100, 9999)}'
    if style == 2:
        return f'{n}{random.randint(1000, 99999)}{random.choice(string.ascii_lowercase)}'
    return f'{a}_{n}{random.randint(1, 99)}'

def jreq(url, body=None, hdrs=None, timeout=30):
    h = {'User-Agent': UA, 'Content-Type': 'application/json', 'Accept': 'application/json'}
    h.update(hdrs or {})
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=h, method='POST' if body is not None else 'GET')
    try:
        resp = urllib.request.urlopen(r, timeout=timeout)
        return resp.status, resp.read().decode('utf-8', 'ignore')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'ignore')[:500]

def post_form(url, fields, auth=None):
    boundary = '----WKB' + ''.join(random.choices(string.ascii_letters + string.digits, k=16))
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}--\r\n'.encode())
    h = {'User-Agent': UA, 'Content-Type': f'multipart/form-data; boundary={boundary}',
         'Origin': 'https://pointgpt.ai', 'Referer': 'https://pointgpt.ai/studio/'}
    if auth:
        h['Authorization'] = 'Bearer ' + auth
    r = urllib.request.Request(url, data=b''.join(parts), headers=h, method='POST')
    try:
        resp = urllib.request.urlopen(r, timeout=60)
        return resp.status, resp.read().decode('utf-8', 'ignore')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'ignore')[:500]

def mailtm_account():
    # returns (addr, pw, mt_token) or (None, None, err); err=='429' signals rate limit
    for attempt in range(5):
        addr = human_user() + '@' + DOMAIN
        pw = 'Fx!' + ''.join(random.choices(string.ascii_letters + string.digits, k=12))
        st, b = jreq('https://api.mail.tm/accounts', {'address': addr, 'password': pw})
        if st == 201:
            st2, b2 = jreq('https://api.mail.tm/token', {'address': addr, 'password': pw})
            if st2 != 200:
                return None, None, f'tok {st2}'
            return addr, pw, json.loads(b2)['token']
        if st == 429:
            time.sleep(50 + attempt * 30)
            continue
        if st == 422:  # bad username — regenerate
            time.sleep(2)
            continue
        return None, None, f'acct {st} {b[:120]}'
    return None, None, '429'

def wait_code(mt_token, tries=40, delay=3):
    for _ in range(tries):
        st, b = jreq('https://api.mail.tm/messages?page=1', hdrs={'Authorization': 'Bearer ' + mt_token})
        try:
            data = json.loads(b)
            msgs = data.get('hydra:member', []) if isinstance(data, dict) else data
        except Exception:
            msgs = []
        if msgs:
            st2, b2 = jreq('https://api.mail.tm/messages/' + msgs[0]['id'], hdrs={'Authorization': 'Bearer ' + mt_token})
            try:
                m = json.loads(b2)
                text = (m.get('text') or '') + ' '.join(m.get('html') or [])
                for c in re.findall(r'\b(\d{4,8})\b', text):
                    if len(c) == 6:
                        return c
            except Exception:
                pass
        time.sleep(delay)
    return None

def farm_one():
    addr, pw, mt = mailtm_account()
    if not addr:
        return {'status': 'mail_fail', 'err': mt}
    st, b = post_form('https://pointgpt.ai/api/site-auth/email/request-code',
                      {'email': addr, 'create_user_if_missing': 'true'})
    if st != 200:
        return {'status': 'req_fail', 'email': addr, 'err': f'{st} {b[:150]}'}
    code = wait_code(mt)
    if not code:
        return {'status': 'no_code', 'email': addr, 'pw': pw}
    ik = ''.join(random.choices(string.hexdigits.lower(), k=32))
    st, b = post_form('https://pointgpt.ai/api/site-auth/email/verify',
                      {'email': addr, 'code': code, 'idempotency_key': ik})
    if st != 200:
        return {'status': 'verify_fail', 'email': addr, 'err': f'{st} {b[:150]}'}
    v = json.loads(b)
    tok = v['auth_token']
    spin = None
    st, b = post_form('https://pointgpt.ai/api/miniapp/me/wheel/spin', {}, auth=tok)
    if st == 200:
        try:
            spin = json.loads(b).get('balance_after')
        except Exception:
            pass
    st, b = jreq('https://pointgpt.ai/api/miniapp/me/wheel', hdrs={'Authorization': 'Bearer ' + tok})
    bal = None
    if st == 200:
        try:
            bal = json.loads(b).get('balance')
        except Exception:
            pass
    rec = {'status': 'ok', 'email': addr, 'mail_pw': pw, 'token': tok,
           'expires': v.get('expires_at'), 'balance': bal, 'after_spin': spin,
           'host': os.environ.get('PG_HOST', 'home'), 'ts': int(time.time())}
    with open(BANK, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    return rec

def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    ok = fail = 0
    consec429 = 0
    for i in range(n):
        try:
            r = farm_one()
        except Exception as e:
            r = {'status': 'exc', 'err': str(e)[:150]}
        if r['status'] == 'ok':
            ok += 1
            consec429 = 0
            print(f"[{i+1}/{n}] OK {r['email']} bal={r['balance']} spin->{r['after_spin']}", flush=True)
        else:
            fail += 1
            print(f"[{i+1}/{n}] FAIL {r}", flush=True)
            if r.get('err') == '429':
                consec429 += 1
                if consec429 >= 3:
                    print('mail.tm 429 x3 — stop', flush=True)
                    break
            else:
                consec429 = 0
        time.sleep(random.uniform(2, 5))
    print(f"DONE ok={ok} fail={fail} bank={BANK}", flush=True)

if __name__ == '__main__':
    main()
