# PointGPT.ai — протокол (реверс 22.10.2026)

Next.js SPA (`/studio/`), API-клиент в чанках `_next/static/chunks/`.

## Auth (email OTP, без капчи)

| Шаг | Метод | Body |
|-----|-------|------|
| Запрос кода | `POST /api/site-auth/email/request-code` | form: `email`, `create_user_if_missing=true` |
| Верификация | `POST /api/site-auth/email/verify` | form: `email`, `code` (6 цифр, TTL 600s), `idempotency_key` |
| Сессия | `GET /api/site-auth/session` | Bearer token |

Ответ verify: `{"ok":true,"auth_token":"...","expires_at":"...+30d","is_new_user":true}`.

**КРИТИЧНО:** все auth-запросы — `multipart/form-data` (клиентский helper `V()` = FormData). С `application/json` сервер отвечает `400 email_invalid` даже для валидного адреса — это НЕ фильтр доменов.

Disposable-домены (mail.tm и пр.) принимаются, MX/deliverability не проверяется.

Альтернативные входы: Telegram-бот `@GPT_onlinebot` (`/api/site-auth/telegram/*`), VK, Yandex (`/api/site-auth/config`).

## Экономика

- Регистрация: 12 монет
- Рулетка: `GET /api/miniapp/me/wheel` (призы 2/5/10/20/50/100/300 монет + Pro 30д), `POST /api/miniapp/me/wheel/spin` — 1 раз/день (сброс 00:00 MSK)
- Free-текст: `GET /api/miniapp/me/free-gpt` → `{"limit":5,"scope":"all_text_models","resets_at":"21:00Z"}` — 5 запросов/день на любую из 45 моделей, `charged_cost=0`
- Ачивки: `GET /api/miniapp/me/achievements` (profile +20 монет, first_generation +2 спина, publish/likes — монеты+спины), claim: `POST /api/miniapp/me/achievements/{key}/claim`
- Цены: `GET /api/miniapp/generation-pricing` (текст 1–2.5, Nano Banana 2 = 1, NB Pro = 4, Seedance 2 Mini 480p/4s = 8, Veo 3.1 = 64, Suno V5 = 20)

## Генерация

- Текст: `POST /api/miniapp/text/generate` — form: `prompt`, `model`, `response_format=text`, `response_length`, `persona=universal`, `context_enabled`, `context_messages_json=[]`, `serial_*`, `studio_chat_id`, `client_request_id`
- Картинки: `POST /api/miniapp/image/generate` — form: `prompt`, `model`, `count`, `ratio`, `style=standard`
- Видео: `POST /api/miniapp/video/generate`
- Гостевой режим (без рега): `POST /api/studio/guest/text|image` + `/api/studio/guest/state` (нужен валидный `studio_guest_id`)

Ответ текста: `{"ok":true,"model_key":...,"answer":"...","charged_cost":0.0,"remaining_coins":N,"generation_request_id":N}`.

## API-ключи

`/api/miniapp/me/api-keys` — Pro-only (на free: GET 404, POST 405). Интеграции только через `auth_token`.

## Модерация

`400 text_moderation_failed` бывает ложным на нейтральных промптах — ретрай с переформулировкой проходит.

## Pitfalls

1. JSON вместо FormData → `email_invalid`
2. `GET /api/miniapp/bootstrap` → 404 (это POST для TG-miniapp `init_data`)
3. mail.tm `422 username not valid` на машинно-случайные имена → человекоподобные юзернеймы
4. mail.tm `429` → бэкофф 50–170с, ~1 акк/мин/IP
