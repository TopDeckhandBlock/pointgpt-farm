#!/bin/bash
# install pg_quota_gate: systemd unit + Caddy handle blocks in front of new-api
set -e
mkdir -p /opt/pg-quota
install -m 755 /root/pg_quota_gate.py /opt/pg-quota/pg_quota_gate.py

cat > /etc/systemd/system/pg-quota.service <<'EOF'
[Unit]
Description=PointGPT per-key daily media quota gate (in front of new-api)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/pg-quota/pg_quota_gate.py
Environment=PG_DAILY_PER_KEY=5
Environment=PGQ_UPSTREAM=http://127.0.0.1:48001
Environment=PGQ_STATE=/opt/pg-quota/quota.json
Restart=always
RestartSec=3
StandardOutput=append:/var/log/pg-quota.log
StandardError=append:/var/log/pg-quota.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable pg-quota >/dev/null 2>&1 || true
systemctl restart pg-quota
sleep 2
systemctl is-active pg-quota

cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-pgquota-$(date +%s)
python3 - <<'PY'
p = '/etc/caddy/Caddyfile'
s = open(p).read()
if '127.0.0.1:8806' in s:
    print('caddy: already patched')
    raise SystemExit
block = open('/root/quota_caddy_block.txt').read().rstrip('\n') + '\n'
i = s.find('api.reformboss.com {')
if i < 0:
    raise SystemExit('api block not found')
j = s.find('\thandle {\n', i)
if j < 0:
    raise SystemExit('handle anchor not found')
s = s[:j] + block + s[j:]
open(p, 'w').write(s)
print('caddy: patched at offset', j)
PY

caddy validate --config /etc/caddy/Caddyfile 2>&1 | tail -3
systemctl reload caddy
sleep 1
curl -s -o /dev/null -w 'quota_health:%{http_code}\n' -m 5 http://127.0.0.1:8806/__quota
echo INSTALL_OK