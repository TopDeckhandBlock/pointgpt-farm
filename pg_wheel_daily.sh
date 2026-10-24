#!/bin/bash
# Daily wheel spin over live PointGPT accounts. Timer: 21:15 UTC = 00:15 MSK (after 00:00 reset).
set -u
cd /root
D=$(date -u +%F)
echo "=== $(date -u +%FT%TZ) start" >> /root/wheel_daily.log
# 1) claim achievements (spins/coins for completed tasks)
python3 -u /root/pg_claim.py /root/bank221.jsonl "/root/claim_${D}.jsonl" 0.35 >> /root/wheel_daily.log 2>&1

# 2) spin the daily wheel
python3 -u /root/pg_wheel3.py /root/bank221.jsonl "/root/wheel_daily_${D}.jsonl" 2 >> /root/wheel_daily.log 2>&1

# 3) refresh real balances (health/memory lie after a 402)
python3 -u /root/pg_refresh_balance.py /root/bank221.jsonl "/root/bank221_live_${D}.jsonl" 0.5 >> /root/wheel_daily.log 2>&1
cp -f "/root/bank221_live_${D}.jsonl" /root/bank221.jsonl

# 4) ship the fresh bank to the shim host
if [ -f /root/ship_bank.sh ]; then bash /root/ship_bank.sh >> /root/wheel_daily.log 2>&1; fi
echo "=== $(date -u +%FT%TZ) done" >> /root/wheel_daily.log
# keep 14 days
ls -1t /root/wheel_daily_*.jsonl 2>/dev/null | tail -n +15 | xargs -r rm -f