#!/usr/bin/env python3
"""Inspect Gmail inbox: last N messages (From/To/Subject) + count of pointgpt mail."""
import email, imaplib, json, os, sys

cfg = sys.argv[1] if len(sys.argv) > 1 else "/root/pg_mail.json"
d = json.load(open(cfg))
c = (d.get("mail") or {}).get("catchall") or {}
user = c.get("gmail_user") or d.get("gmail_user")
pw = c.get("gmail_app_pass") or d.get("gmail_app_pass")
print("user", user, "pw_len", len(pw or ""))
M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
M.login(user, pw)
typ, boxes = M.list()
print("boxes", len(boxes or []))
M.select("INBOX")
typ, data = M.search(None, "ALL")
ids = data[0].split()
print("total", len(ids))
for i in ids[-8:]:
    typ, msg = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
    if typ == "OK" and msg and msg[0]:
        h = msg[0][1].decode("utf-8", "replace").replace("\r\n", " | ")[:150]
        print(i.decode(), h)
# count pointgpt
typ, data = M.search(None, '(FROM "pointgpt")')
print("pointgpt_msgs", len((data[0].split() if data and data[0] else [])))
M.logout()