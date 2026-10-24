#!/usr/bin/env python3
"""Keep PointGPT channel abilities enabled.

new-api (and stray sweep scripts) flip abilities.enabled to 0 when a slow
generation trips a health check. The models then vanish from /v1/models and
every client gets 503 "model unavailable" while the shim is healthy.

Run from a 1-minute systemd timer. Prints a line only when it re-enables rows.
"""
import sqlite3, sys, time

DB = "/opt/grok-gateway/new-api/one-api.db"
CHANNELS = (27034, 27322)
ql = ",".join(str(c) for c in CHANNELS)

con = sqlite3.connect(DB, timeout=30)
con.execute("PRAGMA busy_timeout=30000")
off = con.execute(
    "SELECT channel_id,model,count(*) FROM abilities WHERE channel_id IN (%s) AND enabled=0 GROUP BY channel_id,model" % ql
).fetchall()
if not off:
    sys.exit(0)
con.execute("UPDATE abilities SET enabled=1 WHERE channel_id IN (%s) AND enabled=0" % ql)
con.commit()
print("[pg-abil-guard] %s re-enabled: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"),
      ", ".join("%s/%s" % (c, m) for c, m, _ in off)))