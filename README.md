# pointgpt-farm

Фарм бесплатных аккаунтов [pointgpt.ai](https://pointgpt.ai/) — агрегатора AI-моделей (GPT-6 Astra, Claude Opus 5, Gemini 4 Argon, Nano Banana 2, Seedance/Veo и др.).

Только stdlib Python 3.9+. Без капчи: регистрация = email OTP.

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
| `GET /v1/video/tasks/{id}` | кэш результатов |
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
```

**Подключение к new-api (one-api форк):** канал `type=1` (OpenAI), `base_url=http://127.0.0.1:PORT`, ключ любой (шим его не проверяет), `models` — публичные имена, `model_mapping` — в pointgpt-ключи (`{"nano-banana":"nanobanano-2"}`), в `abilities` вписать модель × все группы юзеров, `test_model=gpt-6-astra` (бесплатный текст для пинга), `auto_ban=0`.

**Деплой systemd:** `WorkingDirectory=/opt/pg-shim`, `Environment=PG_BANK=/opt/pg-shim/master_bank.jsonl PG_PORT=8799`, `ExecStart=/usr/bin/python3 /opt/pg-shim/pg_shim.py`, `Restart=always`. Банк можно обновлять на лету — шим перечитывает файл по mtime.

**Цены (монеты pointgpt):** фото nanobanano-2 = 1, gpt-image-2.5 = 4, seedream-5 = 2; видео minimax-h3 768p/5s = 10, seedance-2-mini 480p/4s = 8, Veo 3.1 = 64, Kling 3 = 50. Пул из N аккаунтов = N × (12 стартовых + ~2-30/день с рулетки) монет.

## Протокол

Подробности: [PROTOCOL.md](PROTOCOL.md)

## Лимиты

- mail.tm: ~1 аккаунт/мин на IP (429 -> бэкофф вшит). Масштаб = несколько хостов/VPS.
- Токен живёт 30 дней, free-квота сбрасывается ежедневно.

## Дисклеймер

Для образовательных целей. Соблюдайте ToS сервиса.
