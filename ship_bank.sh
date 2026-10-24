#!/bin/bash
# Ship the fresh PointGPT bank to the shim host and reload the pool.
# pg_shim hot-reloads the bank file on mtime change; restart is a safe fallback.
set -u
BANK_SRC="${1:-/root/bank221_live.jsonl}"
SHIM_HOST="${SHIM_HOST:-root@2.27.98.196}"
SHIM_BANK="${SHIM_BANK:-/opt/pg-shim/master_bank.jsonl}"

if [ ! -s "$BANK_SRC" ]; then
  echo "ship_bank: missing $BANK_SRC" >&2
  exit 1
fi

N=$(wc -l < "$BANK_SRC")
ssh -o ConnectTimeout=15 -o BatchMode=yes "$SHIM_HOST" \
  "cp -f $SHIM_BANK $SHIM_BANK.bak-ship-\$(date +%s) && cat > $SHIM_BANK" < "$BANK_SRC"

if [ $? -ne 0 ]; then
  echo "ship_bank: upload failed" >&2
  exit 1
fi

echo "ship_bank: shipped $N rows to $SHIM_HOST:$SHIM_BANK"