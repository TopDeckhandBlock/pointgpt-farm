## 5. Per-key daily quota gate sits in front of new-api (:8806)
The coin pool is finite: 172 alive accounts yield roughly one wheel spin each per day
(~+3300 coins/day), while a single unrestricted client burned 1603 coins in two hours.
`PG_RATE_PER_MIN` (20/min) only smooths bursts; it does not cap a day.

`pg_quota_gate.py` listens on `127.0.0.1:8806`, sits between Caddy and new-api
(`PGQ_UPSTREAM=http://127.0.0.1:48001`), counts `POST /v1/images/generations` and
`POST /v1/video/generations` per `Authorization` key per UTC day, and returns
`429 {"code":"pg_daily_quota"}` after `PG_DAILY_PER_KEY` (default 5). GET (video polling)
is never counted. State: `/opt/pg-quota/quota.json`, reset at 00:00 UTC.
Inspect a key: `curl -H "Authorization: Bearer <key>" http://127.0.0.1:8806/__quota`.
Caddy wiring is `quota_caddy_block.txt`, inserted before `handle {}` in the
`api.reformboss.com` block; `/v1/audio/generations` stays on `:8805` (already charges 20).

## 6. Local-to-remote stdin pipe delivers 0 bytes
`cat file | ssh host 'cat > file'` (and base64/tr variants) writes a 0-byte file on this
machine, which silently breaks `git push` with
`remote: Invalid username or token`. Use `scp file host:/path` and verify with
`wc -c` before relying on the content. the keep-alive socket and the
next request becomes `{}POST /v1/audio/generations` -> 501 Unsupported method.

## 3. Balance must be refreshed, not trusted
`/health` keeps stale values after a 402 (balance zeroed in memory). Use
`pg_refresh_balance.py` to re-read `/api/miniapp/me`; the real pool was 546 while health
reported 818/776.

## 4. Video is a long synchronous job
`/v1/video/generations` returns a task id; polling `/v1/videos/<id>` stays at `in_progress 50`
for ~5-6 minutes before `completed 100` with an mp4 URL. Client timeouts below 300s
look like failures but the job still completes upstream.
