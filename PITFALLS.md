# Pitfalls (production-proven)

## 1. toolgate disables media models every 5 minutes
`/root/toolgate.py` sets `abilities.enabled=0` for every model outside its `ALLOW` set.
PointGPT media models were missing -> all 9 dropped every 5 min -> clients saw short 503
windows even with a healthy pool. Add them to ALLOW:

```
nano-banana, gpt-image-2.5, seedream-5, z-image, poyo-sumo-v5,
minimax-h3, seedance-2-mini, nanobanano-2, nanobanano-2-2k
```

Detect who disables abilities with a sqlite trigger:

```sql
CREATE TABLE IF NOT EXISTS ab_log_hist(ts TEXT, cid INT, model TEXT);
CREATE TRIGGER ab_dis BEFORE UPDATE ON abilities
WHEN OLD.enabled=1 AND NEW.enabled=0
BEGIN INSERT INTO ab_log_hist VALUES(datetime('now'),OLD.channel_id,OLD.model); END;
```

## 2. new-api has no /v1/audio/generations route
Music lives on the shim (`POST /v1/audio/generations`, Suno V5, 20 coins, ~40-60s sync).
new-api returns 404 for that path. Run `pg_audio_gate.py` (127.0.0.1:8805): it validates
the caller key against new-api `/v1/models` (cache 120s) then proxies to the shim.
Caddy: `handle /v1/audio/generations { reverse_proxy 127.0.0.1:8805 }`.

Gate pitfall: always read the request body before answering 401 and send
`Connection: close`; otherwise the leftover body sticks to the keep-alive socket and the
next request becomes `{}POST /v1/audio/generations` -> 501 Unsupported method.

## 3. Balance must be refreshed, not trusted
`/health` keeps stale values after a 402 (balance zeroed in memory). Use
`pg_refresh_balance.py` to re-read `/api/miniapp/me`; the real pool was 546 while health
reported 818/776.

## 4. Video is a long synchronous job
`/v1/video/generations` returns a task id; polling `/v1/videos/<id>` stays at `in_progress 50`
for ~5-6 minutes before `completed 100` with an mp4 URL. Client timeouts below 300s
look like failures but the job still completes upstream.
