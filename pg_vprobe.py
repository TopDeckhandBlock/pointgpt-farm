#!/usr/bin/env python3
"""Direct PointGPT video probe with one token (multipart, no deps)."""
import json, sys, urllib.request

bank = sys.argv[1] if len(sys.argv) > 1 else "/root/e149.jsonl"
model = sys.argv[2] if len(sys.argv) > 2 else "minimax-h3"
rows = [json.loads(l) for l in open(bank, encoding="utf-8")]
t = (rows[-1].get("token") or rows[-1].get("auth_token"))
b = "----pg" + "a" * 14
fields = {
    "prompt": "red cube spinning on white table", "model": model, "mode": "video",
    "ratio": "16:9", "duration": "5", "generate_audio": "true",
    "submit_to_showcase": "false", "submit_title": "", "author_nickname": "",
    "history_source": "", "history_title": "", "prompt_template_token": "",
    "showcase_link_token": "", "showcase_link_item_id": "0",
}
parts = []
for k, v in fields.items():
    parts.append('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v))
body = ("".join(parts) + "--%s--\r\n" % b).encode()
req = urllib.request.Request(
    "https://pointgpt.ai/api/miniapp/video/generate", data=body,
    headers={"Authorization": "Bearer " + t,
             "Content-Type": "multipart/form-data; boundary=" + b,
             "User-Agent": "Mozilla/5.0 Chrome/131.0",
             "Origin": "https://pointgpt.ai", "Referer": "https://pointgpt.ai/studio/"})
try:
    x = urllib.request.urlopen(req, timeout=180)
    print("OK", x.status, x.read()[:200].decode("utf-8", "replace"))
except Exception as e:
    code = getattr(e, "code", "")
    raw = b""
    try:
        raw = e.read()[:300]
    except Exception:
        pass
    print("FAIL", type(e).__name__, code, raw.decode("utf-8", "replace")[:300])