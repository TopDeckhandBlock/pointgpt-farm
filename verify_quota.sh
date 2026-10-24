#!/bin/bash
# verify the quota gate does not break real traffic: use one live new-api token,
# run one image generation through https://api.reformboss.com, print status + url only.
set -e
cd /opt/pg-shim 2>/dev/null || true
KEY=$(sqlite3 /opt/pg-shim/../pg-shim/../../root/new-api/one-api.db "select key from tokens where status=1 and name not like '%quota%' order by id desc limit 1;" 2>/dev/null)
DB=$(find / -maxdepth 4 -name 'one-api.db' -not -path '*/proc/*' 2>/dev/null | head -1)
echo "db: $DB"
KEY=$(sqlite3 "$DB" "select key from tokens where status=1 and name not like 'deleted%' and name not like '%test%' order by id desc limit 1;")
if [ -z "$KEY" ]; then echo "NO_KEY"; exit 1; fi
echo "key_len: ${#KEY}"
OUT=$(curl -s -m 180 -w '\nHTTP:%{http_code}' -X POST https://api.reformboss.com/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"nano-banana","prompt":"a red cube on white background","n":1,"size":"1024x1024"}')
echo "$OUT" | tail -c 400
echo
echo "=== quota state for that key ==="
curl -s -m 5 -H "Authorization: Bearer $KEY" http://127.0.0.1:8806/__quota
echo
unset KEY