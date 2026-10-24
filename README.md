# pointgpt-farm

Фарм бесплатных аккаунтов [pointgpt.ai](https://pointgpt.ai/) — агрегатора AI-моделей (GPT-6 Astra, Claude Opus 5, Gemini 4 Argon, Nano Banana 2, Seedance/Veo и др.).

Только stdlib Python 3.9+. Без капчи. Два пути регистрации:
- **Telegram-вход** (основной, работает всегда): `pg_tgfarm.py` — аккаунт pointgpt создаётся одним `/start` боту `@GPT_onlinebot`, почта не нужна.
- **email OTP** (`pg_farm.py` / массовый `pg_emailfarm2.py`): работает и ДЕШЕВЛЕ по IP — Gmail с +алиасами, капчи нет. На 23.10.2026 подтверждено: 5/5 аккаунтов за 81 с, каждый даёт 12 монет.
  Ключевой момент: OTP читается **прямым IMAP-поиском по алиасу** (`TO "prefix+tag@gmail.com"`), а не общим ридером по последним письмам — иначе флуд в ящике съедает окно и получаешь `[NOCODE]`.
- **Ротация по хостам** (`pg_rotator.py`): PointGPT банит IP после ~15-25 регистраций. Ротатор гоняет волны по списку VPS, паркует хост на 40 минут после серии отказов и стягивает банки локально.

## Быстрый старт (3 шага)

```bash
# 1. нафармить аккаунты (банк pointgpt_bank.jsonl создастся сам)
python3 pg_farm.py 20

# 2. слить банки в master (если фармил с нескольких хостов)
python3 merge_bank.py master_bank.jsonl pointgpt_bank.jsonl

# 2b. массовый email-путь: 12 акков с хоста, 2 воркера, банк на хосте
PG_BANK=/root/pg_email_bank.jsonl python3 pg_emailfarm2.py 12 2 /root/pg_mail.json
# 2c. ротация волн по всем хостам (парковка сожжённых IP, стягивание банков)
python3 pg_rotator.py 6 12

# 3. поднять OpenAI-совместимый шлюз и дёргать как обычный OpenAI API
PG_BANK=master_bank.jsonl PG_PORT=8797 python3 pg_shim.py
curl -s http://127.0.0.1:8797/v1/models
```

## Что даёт аккаунт

- **12 монет** на старте + ежедневная рулетка (2–300 монет, иногда Pro 30д)
- **5 бесплатных запросов в день на ВСЕ 45 текстовых моделей** (включая `gpt-6-astra`, `claude-opus-5`, `claude-sonnet-5`, `gemini-4-argon`, `gpt-5.6-sol`, `deepseek-v4-pro`, `kimi-k3`), сброс 21:00 UTC
- Сверх квоты: текст 1–2.5 монеты, картинка Nano Banana 2 = 1 монета, видео Seedance 2 Mini 480p/4s = 8 монет

## Запуск

```bash
python3 pg_farm.py 10                # 10 аккаунтов
PG_BANK=/path/bank.jsonl python3 pg_farm.py 50
```

Каждый аккаунт: создание ящика mail.tm -> запрос кода pointgpt -> чтение кода из inbox -> verify -> auth_token (30 дней) -> спин рулетки -> запись в банк.

Банк (`pointgpt_bank.jsonl`): `{"email","mail_pw","token","expires","balance"}`.

## Ферма через Telegram (без почты) — pg_tgfarm.py

Регистрация без email: `POST /api/site-auth/telegram/start` -> бот `@GPT_onlinebot` получает `/start web_login_<challenge_id>` -> `GET /api/site-auth/telegram/status?challenge_id=..&poll_token=..` -> `auth_token` (TTL 30 дней). Дальше `GET /api/miniapp/me` + рулетка -> банк.

```bash
export PG_SESS=./tgsess            # папка с .session Telethon (пул TG-аккаунтов)
export PG_BANK=./pointgpt_bank.jsonl
export PG_STATE=./pg_tg_state.json # прогресс: какие сессии уже использованы
export PG_WORK=6                   # параллельных воркеров
export PG_SOCKS=127.0.0.1:1080,127.0.0.1:1081   # опционально: HTTP через SOCKS (свой VPS), Telethon идёт напрямую
python3 pg_tgfarm.py               # или PG_LIMIT=50 python3 pg_tgfarm.py
```

Требуется `pip install telethon>=1.44` (единственная внешняя зависимость, только для TG-фермы).

Замечания по живому прогону:
- одна TG-сессия = один аккаунт pointgpt навсегда (повторный `/start` создаёт новый аккаунт только для новой сессии);
- сессии из чужих пулов копируй к себе (`cp -r`), не используй оригинал — иначе AuthKeyDuplicated убивает ключ;
- после ~100 регистраций с одного IP pointgpt начинает отбрасывать соединения (`10060`, либо мгновенный RST → `Connection refused`) — гоняй HTTP через SOCKS-выход на VPS (`PG_SOCKS=127.0.0.1:1080`), Telethon при этом работает напрямую;
- тот же бан ловит и шим: если `/v1/images/generations` внезапно отдаёт 502/401 «Генерация временно недоступна» при живом банке — значит IP шима забанен. Лечение: `Environment=PG_SOCKS=127.0.0.1:1081` в unit шима (поддержка `PG_SOCKS` встроена) + туннель `ssh -N -D 127.0.0.1:1081 root@<чистый-vps>`;
- часть сессий мёртвая (`dead`), часть требует повторного логина (`noauth`) — это норма, state-файл позволяет догонять пачками;
- статус `ok` никогда не затирается неудачей (внутри `put_state`), иначе повторный прогон по уже отработанной сессии терял бы токен из state (сам токен при этом остаётся в банке);
- пул масштабируется копированием любых своих `.session` в `PG_SESS`; дедуп по md5 файла обязателен — два одинаковых auth_key = `AuthKeyDuplicated` и смерть сессии.

## Использование токена

```bash
curl -s https://pointgpt.ai/api/miniapp/me/free-gpt -H "Authorization: Bearer $TOKEN"

# текстовая генерация (multipart/form-data ОБЯЗАТЕЛЬНО, JSON = email_invalid)
curl -s -X POST https://pointgpt.ai/api/miniapp/text/generate \
  -H "Authorization: Bearer $TOKEN" \
  -F "prompt=Hello" -F "model=gpt-6-astra" -F "response_format=text" \
  -F "response_length=medium" -F "persona=universal" \
  -F "context_enabled=false" -F "context_messages_json=[]"
```

## Переделка во внутренний OpenAI-compatible API (pg_shim.py)

Внутренний API pointgpt не совместим с OpenAI (multipart/form-data, Bearer auth_token, свои имена моделей и поля). `pg_shim.py` — stdlib-шлюз, который превращает пул фарм-аккаунтов в обычную OpenAI-совместимую апишку.

**Как работает:**
1. Грузит банк `PG_BANK` (jsonl из pg_farm) — пул `auth_token` с балансами.
2. На каждый запрос выбирает живой аккаунт (ротация по балансу, карантин на 401/403/429, авто-скип мёртвых).
3. Конвертирует OpenAI JSON -> multipart/form-data pointgpt и обратно.
4. Балансы/статистика обновляются в рантайме (`GET /health`).

**Эндпоинты шима:**

| Шим (OpenAI-style) | Upstream pointgpt |
|---|---|
| `GET /v1/models` | пул + каталог моделей |
| `POST /v1/chat/completions` (stream/non-stream) | `POST /api/miniapp/text/generate` |
| `POST /v1/images/generations` -> `data[0].url` | `POST /api/miniapp/image/generate` |
| `POST /v1/video/generations` (sync, 2-4 мин) -> `{task_id, status, video_url}` | `POST /api/miniapp/video/generate` |
| `POST /v1/videos` (Sora-style, async) -> `{id, status: queued}` | фон: `do_video` в треде |
| `GET /v1/videos/{id}` (poll) -> `status/video_url/assets[0].url`, `/content` -> 302 на mp4 | кэш результатов |
| `GET /v1/video/tasks/{id}` | кэш результатов |
| `POST /v1/audio/generations` (Suno V5, sync ~40-60 с) -> `data[0].url` (mp3) | `POST /api/miniapp/audio/generate` |
| `GET /v1/audio/tasks/{id}` | кэш результатов |
| `GET /health` | пул/балансы/стата |

**Запуск:**

```bash
PG_BANK=/path/master_bank.jsonl PG_PORT=8797 python3 pg_shim.py
```

**Примеры:**

```bash
# фото (nanobanano-2 = 1 монета, ~15 с)
curl -s http://127.0.0.1:8797/v1/images/generations -H "Content-Type: application/json" \
  -d '{"model":"nanobanano-2","prompt":"pixel-art ghost over neon city"}'

# видео (minimax-h3 768p/5s = 10 монет, sync ~3 мин; seedance-2-mini = 8 монет, generate_audio только true)
curl -s -m 600 http://127.0.0.1:8797/v1/video/generations -H "Content-Type: application/json" \
  -d '{"model":"minimax-h3","prompt":"neon city rain cinematic","duration":5,"ratio":"16:9"}'

# музыка (poyo-sumo-v5 = Suno V5, 20 монет, ~40-60 с; with_lyrics=false = инструментал)
curl -s -m 420 http://127.0.0.1:8797/v1/audio/generations -H "Content-Type: application/json" \
  -d '{"model":"poyo-sumo-v5","prompt":"dark synthwave, driving bass","title":"Neon Drive","with_lyrics":false}'

# музыка через обычный chat completions (для new-api/one-api форков без audio-relay)
# ответ: content = "Track ready: <mp3 url>", pg_meta.audio_url
curl -s -m 420 http://127.0.0.1:8797/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"poyo-sumo-v5","messages":[{"role":"user","content":"dark synthwave, driving bass"}]}'
```

**Проверенные модели (живой апстрим, 23.10.2026):**

| Тип | Работает | Не работает |
|---|---|---|
| Фото | `nanobanano-2`, `nanobanano-2-2k`, `gpt-image-2.5`, `seedream-5`, `z-image` | `nanobanano-2-4k` (500, нестабильно), `nano-banana` (такого имени у апстрима нет — `unsupported_model`), `midjourney` (402: не хватает монет) |
| Видео | `minimax-h3` (~200 c, отдаёт mp4) | `seedance-2-mini` («Генерация временно недоступна»), `grok`/`veo`/`wan-3-0` — нет на free-пуле |
| Музыка | `poyo-sumo-v5` (Suno V5) | — |

**Особенности new-api (проверено на живом форке):**
- **Музыка:** форк не маршрутизирует `/v1/audio/*` на каналы type=1 — музыка доступна через `/v1/chat/completions` с `model=poyo-sumo-v5` (шим сам распознаёт музыкальную модель в chat-маршруте; в ответе `content = "Track ready: <mp3 url>"` + `pg_meta.audio_url`).
- **Фото:** `/v1/images/generations` работает напрямую через канал type=1.
- **Видео:** проверенный рабочий вариант — канал `type=1` (OpenAI) с `base_url` на шим и видео-моделями в `models`+`abilities`: форк релейит `POST /v1/video/generations` в шим, шим отвечает `{task_id, status:queued}`, клиент поллит `GET /v1/videos/{task_id}` до `status=completed` и `assets[0].url` (mp4). ~200 c на ролик. Канал `type=17` (Ali video) держи как резерв (priority выше числом = ниже приоритет).
- Если видео отдаёт 404 «Проверьте название модели» — почти всегда модель не вписана в `abilities` нужной группы либо имя модели не совпадает с именем в шиме (`nanobanano-2`, не `nano-banana`).
- 401/402 от апстрима на видео = мёртвые/пустые аккаунты в банке: обнови банк (свежие токены) и перезапусти шим, чтобы сбросить in-memory метки `dead`.
- **Тот же 401 приходит с забаненного IP.** Симптом: фото отдаёт 200, видео стабильно `401 «Генерация временно недоступна»`. Лечение — вывести шлюз через чистый IP: на отдельном VPS поднять `ssh -N -D 127.0.0.1:1081 root@<clean-host>` (юнит `pg-socks.service`), в шиме выставить `PG_SOCKS=127.0.0.1:1081` и `Wants=pg-socks.service`. Без `authorized_keys` между хостами туннель молча падает (`Permission denied (publickey)`) — добавь pubkey шлюза в `authorized_keys` чистого хоста.
- Шим при 402 «Недостаточно монет» ставит аккаунту баланс 0 и кулдаун 1 ч, а не помечает `dead`; для видео/музыки выборка идёт по минимальному балансу (`minimax-h3` ≈ 10 монет → порог 12).
- Модель-имя в token `model_limits` должна быть разрешена (иначе 403 «модель не разрешена» на poll).

**Подключение к new-api (one-api форк):** канал `type=1` (OpenAI), `base_url=http://127.0.0.1:PORT`, ключ любой (шим его не проверяет), `models` — публичные имена, `model_mapping` — в pointgpt-ключи (`{"nano-banana":"nanobanano-2"}`), в `abilities` вписать модель × все группы юзеров, `test_model=gpt-6-astra` (бесплатный текст для пинга), `auto_ban=0`.

**Деплой systemd:** `WorkingDirectory=/opt/pg-shim`, `Environment=PG_BANK=/opt/pg-shim/master_bank.jsonl PG_PORT=8799`, `ExecStart=/usr/bin/python3 /opt/pg-shim/pg_shim.py`, `Restart=always`. Банк можно обновлять на лету — шим перечитывает файл по mtime.

**Цены (монеты pointgpt):** фото nanobanano-2 = 1, gpt-image-2.5 = 4, seedream-5 = 2; видео minimax-h3 768p/5s = 10, seedance-2-mini 480p/4s = 8, Veo 3.1 = 64, Kling 3 = 50; музыка Suno V5 = 20 за трек (TTS ElevenLabs/Gemini — 10-20 за 1000 символов). Пул из N аккаунтов = N × (12 стартовых + ~2-30/день с рулетки) монет.

## Протокол

Подробности: [PROTOCOL.md](PROTOCOL.md)

## Лимиты

- mail.tm: ~1 аккаунт/мин на IP (429 -> бэкофф вшит), плюс апстрим pointgpt может вообще не отправлять код (`resend_send_failed`).
- Telegram-путь: ограничение — количество живых TG-сессий и бан IP pointgpt после ~100 регистраций (лечится SOCKS-выходом на VPS).
- Токен живёт 30 дней, free-квота сбрасывается ежедневно.

## Файлы

| Файл | Что делает |
|---|---|
| `pg_farm.py` | авторег: mail.tm ящик -> OTP -> auth_token -> рулетка -> банк jsonl |
| `pg_tgfarm.py` | авторег через Telegram-вход (без почты): TG-сессия -> auth_token -> рулетка -> банк |
| `merge_bank.py` | слияние нескольких банков в один master (дедуп по token) |
| `pg_shim.py` | OpenAI-совместимый шлюз: текст/фото/видео/музыка с ротацией пула |
| `PROTOCOL.md` | полный реверс протокола pointgpt (эндпоинты, поля, цены) |

## Лицензия

MIT — см. [LICENSE](LICENSE).

## Дисклеймер

Для образовательных целей. Соблюдайте ToS сервиса.

## Pitfall: new-api watchdog disables the models

`ch-watchdog` (2 strikes / 20 min) deletes `abilities` rows for channels matching
`status=1 AND type=1 AND base_url != ''`. Our gateway channel matches that filter,
so a single slow generation (seedream-5 ~120s, video ~4 min) makes the watchdog wipe
every PointGPT model from `/v1/models` while the shim itself is healthy.

Exclude the channel by name in `_ch_watchdog.py`:

```sql
... AND name NOT LIKE 'pointgpt-%' AND name NOT LIKE 'pg-%'
```

Then re-enable the rows and wait 10-15 min (new-api caches abilities):

```sql
UPDATE abilities SET enabled=1 WHERE channel_id IN (27034,27322);
```

Also keep `do_image` returning a 3-tuple: the caller unpacks `st, d, meta`, so a
2-tuple raises `ValueError`, the shim closes the connection and new-api reports
502/503 even with a full pool.

Refresh real balances before blaming the pool: run `pg_refresh_balance.py`. The shim
zeroes `balance` in memory after a 402, so `/health` understates the pool.
