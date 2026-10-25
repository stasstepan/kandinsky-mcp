# Kandinsky MCP Server — архитектура

**v1.0** · решения согласованы · к реализации · код ещё не написан

---

## 0. Согласованные решения

| № | Вопрос | Решение |
|---|---|---|
| 1 | Деплой | **Один образ, два режима.** Streamable HTTP на VPS: `https://kandinsky.example.com/mcp`. Stdio — для локальной отладки |
| 2 | API | **`kandinskyapi.sberdevices.ru`**, Bearer-токен есть |
| 3 | Объём v1 | **Только изображения:** t2i, i2i, superres. Видео/аватар — задел в реестре маршрутов, но без тулов |
| 4 | Доставка файла | **URL от нашего сервера + inline-превью** картинки в ответе |
| 5 | Контракт | **Асинхронный:** `create` / `status` / `result` + фоновое авто-скачивание внутри сервера |
| 6 | Сборка образа | Dockerfile и compose пишем здесь, **собираем на VPS**. Локально — тесты на bundled Python |

---

## 1. Что выяснено в ходе исследования

### 1.1 Документация API

`инструкция_API_new.pdf` — 8 страниц, извлечён текст (PDF с Type0/Identity-H шрифтом,
кириллица через ToUnicode CMap). Полный текст: [api-extract.txt](api-extract.txt).
Сводная справка по маршрутам: [api-reference.md](api-reference.md).

Суть: **асинхронный** REST API. `POST /tasks/<route>` → `task_id` → поллинг
`GET /tasks/{id}` → `GET /tasks/{id}/result` (бинарник). Авторизация —
`Authorization: Bearer <token>`. Base URL: `https://kandinskyapi.sberdevices.ru`.

### 1.2 Живой OpenAPI-спек

Сервис отдаёт `GET /openapi.json` (OpenAPI 3.1). Это первоисточник, который
уточняет и местами исправляет PDF:

| Что | PDF | OpenAPI (факт) |
|---|---|---|
| Разделитель разрешения | `"1280:768"` | enum `"1280x768"` (через `x`) |
| Статусы | `processing` / `done` | enum `queued · processing · done · failed` |
| HTTP-код статуса | не описано | **202** пока в работе, **200** когда `done`/`failed` |
| `kandinsky6.5-preview-i2va` | `image`: string | `images`: array 1..3 |
| Квоты | не описано | 429 `{"detail":{"code":"LIMIT_EXHAUSTED","type":"daily\|monthly\|concurrent\|rate"}}` |
| Лимит промпта | не описано | 2048 символов, иначе 422 `prompt too long` |
| Лимиты входных картинок | не описано | ≤25 МБ decoded, каждая сторона ≤2048 px (superres: ≤4 МБ, ≤1024 px) |

**Критично для архитектуры — время жизни результата:**

- генерация: таймаут **45 мин** → `failed`;
- окно скачивания: **10 минут** после `done` → иначе `failed` («unpicked»);
- после успешного скачивания медиа удаляется через **60 секунд**;
- повторный `GET /result` → **422**, `GET /status` → `done` + `error` про доставку.

То есть **результат одноразовый**. Скачал — второй раз не скачаешь. Не скачал за
10 минут — потерял. Вокруг этого ограничения строится вся схема доставки.

Полезный побочный факт: по read-эндпоинтам **квота не расходуется** — поллить
можно свободно, что делает фоновый поллер дешёвым.

### 1.3 Два разных хоста API (учтено в реестре)

Оба живы и отдают `openapi.json`, но наборы маршрутов **различаются**: у
`kandinskyapi.ru` есть `k6_t2va_lite` и нет `k6-kandishop-quantized` и
`kandinsky6.5-preview-*`. Выбран `kandinskyapi.sberdevices.ru`, но реестр
маршрутов всё равно сверяется с `/openapi.json` при старте — чтобы смена хоста
или эволюция API не превращались в 404.

### 1.4 Существующий репозиторий `ai-forever/mcp_kandinsky`

Прочитан целиком (README, `server.py`, `kandinsky_api.py`, `pyproject.toml`).
Последний коммит — год назад. Написан **под старый API FusionBrain** и с новым
несовместим по всем ключевым точкам:

| | Старый репозиторий | Новый API |
|---|---|---|
| База | `https://api-key.fusionbrain.ai/` | `https://kandinskyapi.sberdevices.ru` |
| Авторизация | `X-Key: Key …`, `X-Secret: Secret …` | `Authorization: Bearer …` |
| Создание | `POST key/api/v1/pipeline/run`, multipart, `pipeline_id` | `POST /tasks/<route>`, JSON |
| Параметры | `style`, `width`, `height`, `numImages`, `negativePromptDecoder` | `params.query`, `params.resolution`, `params.beautificator`, `params.image` |
| Статус | `DONE` / `FAIL` | `queued` / `processing` / `done` / `failed` |
| Результат | base64 в JSON | бинарный поток `image/png` |
| Модели | одна (K 3.1) | 14 маршрутов |
| Транспорт | только stdio | — |

Переиспользуемо: **каркас** (структура пакета, `pyproject`, схема «промпт → файл»).
Клиент, параметры, ошибки, транспорт — пишутся заново. Это переписывание, а не патч.

Антипаттерн, который **не** тащим: тул блокирующе ждёт генерацию
(`attempts=10, delay=5` → 50 с) и падает в `TimeoutError`. Для картинок это
потеря результата при 429 или сетевом сбое; для видео — гарантированный таймаут.

### 1.5 Куда подключается (агент)

Профиль DSH `~/.dsh/profiles/desktop/cordis.patch.yml` уже использует MCP-серверы
по транспорту `streamable-http` через реверс-прокси:

```yaml
- insert:
    - id: mcp-fs
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: fs
        transport: streamable-http
        url: https://fs.example.com/mcp
        headers:
          X-API-Key: sk-…
        toolCallTimeoutMs: 120000
        failOnStartupError: false
```

Новый сервер встаёт в тот же ряд: `POST /mcp` + `X-API-Key`.

### 1.6 Локальное окружение

- Docker / Docker Desktop — **не установлены**, WSL — **не установлен**;
  сборка образа здесь невозможна (решение: собираем на VPS);
- git, curl — нет в PATH; исходящий TLS у системных утилит падает
  (`schannel: SEC_E_NO_CREDENTIALS`), поэтому GitHub читался через встроенный
  web-фетчер, а не `git clone`;
- Python 3.12 и Node — только внутри рантайма DSH;
- **PyPI доступен** (`pip download httpx` скачал колесо) — значит локальные
  тесты на bundled Python реальны; писать надо в venv внутри рабочей папки,
  потому что системный `%TEMP%` песочницей закрыт.

---

## 2. Архитектурные решения

### 2.1 Ключевая идея: сервер как durable job-менеджер

API-шные «10 минут на скачивание и 60 секунд после» — ловушка для агента: модель
может не поллить вовремя, а ретрай инструмента означает потерянный результат.
Поэтому сервер **не** перекладывает поллинг на агента.

```
create ──► POST /tasks/…  ──► task_id
              │
              ├──► /data/tasks/<id>.json  (метаданные, атомарная запись)
              └──► фоновый поллер (asyncio.Task)
                        │
                        ├─ GET /tasks/{id}  каждые 2→10 с (бэкофф; квота не тратится)
                        ├─ done   → GET /tasks/{id}/result  ОДИН раз, немедленно
                        │           → /data/results/<date>/<id>.<ext> + sha256
                        └─ failed → сохранить error, терминальное состояние
```

Агент в любой момент видит согласованное локальное состояние: `queued` →
`processing` → `saved` / `failed`. Результат уже на диске — окно API неважно.
После рестарта контейнера состояние восстанавливается из `/data/tasks/*.json`,
поллеры перезапускаются для нетерминальных задач.

Следствие: **ни один тул не висит минутами** — таймауты инструмента
(`toolCallTimeoutMs: 120000` в профиле DSH) перестают быть фактором риска.

### 2.2 Общая схема

```
┌────────────────────────────┐
│ Агент (DSH / Cursor / …)   │
└─────────────┬──────────────┘
              │ MCP: Streamable HTTP  POST /mcp  (X-API-Key)
              │ — либо — stdio (docker run -i)
┌─────────────▼──────────────────────────────────────────────┐
│ Контейнер kandinsky-mcp                                    │
│                                                            │
│  MCP-слой (mcp SDK, MCPServer)                             │
│   tools/  — схемы, валидация, тексты ошибок для модели     │
│  ────────────────────────────────────────────────────────  │
│  Сервисный слой                                            │
│   TaskManager   — создание, поллеры, состояние, recovery   │
│   RouteRegistry — реестр маршрутов + сверка с openapi.json │
│   FileStore     — сохранение, TTL, индекс задач            │
│  ────────────────────────────────────────────────────────  │
│  Инфраструктура                                            │
│   KandinskyClient — httpx, Bearer, ретраи, 429             │
│   MediaPrep       — Pillow: проверка/ресайз входных картинок│
│   Auth            — X-API-Key + rate limit                 │
│                                                            │
│  Starlette ASGI:  /mcp   /files/*   /healthz  /readyz      │
│  Volume /data:    tasks/  results/                         │
└─────────────┬──────────────────────────────────────────────┘
              │ HTTPS, Authorization: Bearer <token>
┌─────────────▼──────────────────────────────┐
│ kandinskyapi.sberdevices.ru                │
│  POST /tasks/<route>                       │
│  GET  /tasks/{id}                          │
│  GET  /tasks/{id}/result                   │
└────────────────────────────────────────────┘
```

Слои разделены так, чтобы API-клиент тестировался без MCP, а тулы — без сети.

### 2.3 Два транспорта в одном образе

| Режим | Запуск | Назначение |
|---|---|---|
| `http` (на VPS) | uvicorn `0.0.0.0:8000`, `POST /mcp` + `X-API-Key` | постоянный сервис за nginx на `kandinsky.example.com` |
| `stdio` | `docker run -i --rm -e KANDINSKY_TOKEN=… kandinsky-mcp` | локальная отладка, агенты без сети до VPS |

В stdio-режиме HTTP-роуты не поднимаются; файлы доступны через volume и
`kandinsky://results/<id>` как MCP-ресурс.

### 2.4 Контракт инструментов: асинхронный

По умолчанию `kandinsky_image_generate` возвращает `task_id` сразу. Дальше:

- фоновый поллер (2.1) качает результат сам;
- `kandinsky_task_status` — локальное состояние (+ опционально свежий статус из API);
- `kandinsky_task_result` — **не ходит в API**, отдаёт уже сохранённый файл.

Поэтому ретрай со стороны агента безопасен и не теряет результат. Блокирующих
обёрток в v1 нет: для изображений фон справляется за секунды, а `wait_seconds`
можно добавить позже без ломки контракта.

### 2.5 Доставка результата

```json
{
  "task_id": "1842",
  "status": "saved",
  "model": "k6-image-t2i",
  "prompt": "…",
  "media": [{
    "kind": "image", "mime": "image/png", "bytes": 1482311,
    "sha256": "9f2c…",
    "url": "https://kandinsky.example.com/files/2026-02-14/1842.png?exp=…&sig=…",
    "inline": true
  }],
  "elapsed_ms": 18400,
  "expires_at": "2026-02-21T12:00:00Z"
}
```

- **URL** — основной путь; агент скачивает своими средствами;
- **inline `ImageContent`** — картинки ≤ 1.5 МБ встраиваются в ответ, чтобы клиент
  сразу отрисовал превью, не делая второй запрос;
- ссылка подписана и живёт ограниченное время, чтобы `/files/*` не был открытым.

### 2.6 Хранилище

```
/data
  tasks/<task_id>.json              # состояние, параметры, ошибка, файлы
  results/<YYYY-MM-DD>/<task_id>.png
```

- без БД: JSON + `os.replace` для атомарности;
- TTL `RESULT_TTL_HOURS` (умолчание 168 = 7 дней), GC раз в час;
- `kandinsky_tasks_list` — скан `tasks/`, для сотен файлов дешевле SQLite;
- SQLite добавляется позже без изменения контракта тулов.

### 2.7 Реестр маршрутов

Маршрут описывается данными, а не кодом на каждый случай:

| Поле | Пример (`k6-image-t2i`) |
|---|---|
| `route` | `k6-image-t2i` |
| `kind` | `image` \| `video` \| `video_audio` \| `avatar` |
| `requires` | `query` |
| `allowed` | `resolution`, `beautificator` |
| `enums` | `resolution: [1024x1024, 768x768, 768x1280, auto, 1280x768]` |
| `limits` | `prompt_max: 2048` |
| `output` | `image/png` |

В v1 тулы есть только у `kind=image`; остальные записи реестра присутствуют и
видны в `kandinsky_routes`, но помечены `exposed: false` — это задел на итерацию 2
без переделки.

При старте (`ROUTE_SYNC=on`) сервер тянет `/openapi.json` и сверяет реестр:
чего нет — `unavailable`, чего реестр не знает — warning в лог.

### 2.8 Обработка ошибок

| Ответ API | Код агенту | Подсказка |
|---|---|---|
| 401 / 403 | `auth_failed` | «проверь KANDINSKY_TOKEN» |
| 400 `task_id must be numeric` | `bad_request` | — |
| 404 | `not_found` | «задача не существует или принадлежит другому токену» |
| 413 `too large file` | `input_too_large` | лимит + реальный размер |
| 422 `too big resolution` | `input_bad_resolution` | лимит сторон |
| 422 `prompt too long N>M` | `prompt_too_long` | фактическая длина и предел |
| 422 `… blocked by censor: …` | `censored` | **не ретраить**, переписать промпт |
| 422 `… censor service unavailable` | `censor_unavailable` | ретрай возможен |
| 422 `not ready (status=…)` | `not_ready` | чего ждать |
| 422 «already delivered» | `result_gone` | одноразовая выдача, объяснить |
| 429 `LIMIT_EXHAUSTED` + `type` | `quota_daily` / `quota_monthly` / `quota_concurrent` / `quota_rate` | какой лимит и что делать |
| 5xx / сеть | `upstream_error` | ретрай с бэкоффом |

Ретраи — только для `GET` и для `POST`, пока не получен `task_id`. 429 — уважать
`Retry-After`, иначе экспоненциальный бэкофф.

### 2.9 Валидация и нормализация входа

До обращения к API, чтобы не жечь квоту на заведомо провальном запросе:

- промпт: непустой, trim, длина ≤ лимита маршрута;
- **разрешение: принимать `1024:1024`, `1024x1024`, `1024×1024`** → нормализовать
  в `x` (в PDF `:`, в схеме `x`; модель будет ошибаться — это должно лечиться, а
  не падать); неизвестное значение → вернуть список допустимых;
- входные картинки: формат (JPEG/PNG), размер файла, стороны; больше лимита →
  Pillow: ресайз/пережатие + явное указание в ответе, что картинка уменьшена;
- `upscale`: только 2 или 4; `one_step_t`: 0..1.

### 2.10 Безопасность

- `KANDINSKY_TOKEN` — только из env, никогда в логи и ответы; в логах маска
  (`Bearer …c3f2`); в `.env` на VPS, **не в чат и не в репозиторий**;
- `X-API-Key` на `/mcp`; `/files/*` — подписанный короткоживущий URL;
- rate limit по ключу (token bucket), чтобы один агент не выжег дневную квоту;
- имя файла от агента санитизируется, путь всегда внутри `/data`;
- контейнер: non-root (uid 10001), read-only rootfs, `no-new-privileges`,
  writable только `/data` и `tmpfs /tmp`;
- входа по https-URL в v1 нет (это SSRF-поверхность) — только путь и base64.

### 2.11 Наблюдаемость

- JSON-логи в stdout: `ts, level, tool, task_id, route, status, duration_ms, err_code`;
- `/healthz` — процесс жив; `/readyz` — плюс кешированный (30 с) пинг `GET /health`
  у Kandinsky;
- `/metrics` (Prometheus, опционально): создано/успешно/провалено по маршрутам,
  429, размеры, длительности.

---

## 3. Инструменты v1 (7 штук)

| Инструмент | Маршрут | Параметры |
|---|---|---|
| `kandinsky_image_generate` | `k6-image-t2i` (по умолч.), `k5-image-t2i` | `prompt`* · `model` `k6\|k5` · `resolution` enum · `beautificator` enum · `censor` bool · `guidance`/`use_original_prompt` (только k5) |
| `kandinsky_image_edit` | `k6-i2i` (по умолч.), `k6-kandishop-quantized` | `prompt`* · `images`* 1..3 (путь / base64) · `beautificator` · `censor` |
| `kandinsky_image_upscale` | `k6_superres` | `image`* · `upscale` `2\|4`* · `one_step_t` 0..1 · `censor` |
| `kandinsky_task_status` | — | `task_id`* · `refresh` bool (спросить API, а не локальный кеш) |
| `kandinsky_task_result` | — | `task_id`* · `inline` bool (встраивать превью) |
| `kandinsky_tasks_list` | — | `limit` · `status` · `model` |
| `kandinsky_routes` | — | `kind` (фильтр) · `available_only` bool |

Соглашения: перечисления — строгие `enum` в схеме; ошибки — `is_error=true` с
кодом и подсказкой; тексты тулов двуязычные (RU/EN), чтобы модель одинаково
понимала аргументы.

Отвергнутая альтернатива: один универсальный `kandinsky_generate` с
`params: dict`. Слабая схема → модель путает `image`/`images`,
`resolution`/`aspect_ration`, ошибки ловятся только на сервере.

---

## 4. Стек

| Слой | Выбор | Обоснование |
|---|---|---|
| Язык | Python 3.12 | `Pillow`, зрелый MCP SDK |
| MCP SDK | **`mcp` 2.3.x** (`from mcp.server import MCPServer`) | v2 — стабильная линия, `pip install mcp` даёт 2.x; `FastMCP` → `MCPServer`. Отдаёт и 2025-, и 2026-протокол сразу |
| HTTP-клиент | `httpx` (через SDK — `httpx2`) | в slim-образе обязателен `ca-certificates` |
| ASGI | Starlette + uvicorn | `/mcp` + `/files/*` + healthz в одном приложении |
| Картинки | Pillow | валидация/ресайз входных изображений |
| Конфиг | pydantic-settings | 12-factor |
| Пакеты | `uv` + `uv.lock` | воспроизводимость, быстрый образ |
| Тесты | pytest + pytest-asyncio + respx | мок API |
| Линт | ruff | |

Риск по SDK v2 снимается **первым шагом реализации**: минимальный `MCPServer` с
`streamable-http` + `X-API-Key` + кастомный роут. Если ASGI-интеграция под наши
требования неудобна — откат на `mcp` 1.30.x (`FastMCP`, `streamable_http_app()`
проверен в бою). Решение до написания тулов.

---

## 5. Docker

### 5.1 Образ

- multi-stage: `python:3.12-slim` + `uv` → wheels в builder → чистый runtime;
- `ca-certificates` (обязательно для `httpx2`), `tzdata`;
- non-root `app` (10001), `HEALTHCHECK` на `/healthz`;
- `ENTRYPOINT` — шим: `MCP_TRANSPORT=http` → uvicorn, `stdio` → `python -m kandinsky_mcp`;
- ожидаемый размер ~180–220 МБ.

### 5.2 compose (на VPS)

```yaml
services:
  kandinsky-mcp:
    image: kandinsky-mcp:latest
    restart: unless-stopped
    env_file: .env
    environment:
      MCP_TRANSPORT: http
      MCP_HTTP_PORT: 8000
    volumes: [ "kandinsky-data:/data" ]
    read_only: true
    tmpfs: [ "/tmp" ]
    security_opt: [ "no-new-privileges:true" ]
    deploy: { resources: { limits: { memory: 1g, cpus: "1.0" } } }
volumes: { kandinsky-data: {} }
```

### 5.2.1 Реверс-прокси: Caddy

TLS — автоматический (ACME), конфиг — `deploy/Caddyfile`:

```caddyfile
kandinsky.example.com {
    encode zstd gzip

    # base64 раздувает входные картинки на ~33 %: 25 МБ файл → ~34 МБ JSON
    request_body {
        max_size 40MB
    }

    reverse_proxy 127.0.0.1:8000 {
        # критично для /mcp: без этого Caddy буферизует SSE-поток
        # и MCP-клиент не получает события до закрытия ответа
        flush_interval -1
        transport http {
            read_timeout 0
        }
    }
}
```

Что важно именно для MCP поверх Caddy:

- **`flush_interval -1`** — обязательно. По умолчанию Caddy буферизует ответ, и
  Streamable HTTP «висит» без событий.
- **`request_body { max_size 40MB }`** — иначе большая входная картинка
  упрётся в лимит прокси, а не в лимит нашего API (и агент получит невнятную 413).
- **`read_timeout 0`** — долгие стриминговые ответы не должны обрываться.
- **HTTP/2** для `POST /mcp` работает, но отдельные MCP-клиенты капризничают;
  если увидим обрывы — `protocols h1` только на этом хосте.
- `Mcp-Session-Id`, `X-API-Key` и `Authorization` Caddy пробрасывает без настройки;
  `X-Forwarded-For` проставляется — приложению доверять ему только с `127.0.0.1`.

Если Caddy живёт в отдельном контейнере, `127.0.0.1` заменяется на имя сервиса
(`kandinsky-mcp:8000`) и оба контейнера попадают в одну docker-сеть.

### 5.3 Структура репозитория

```
kandinsky-mcp/
├── pyproject.toml   uv.lock   .python-version
├── Dockerfile   .dockerignore   docker-compose.yml   .env.example
├── deploy/
│   └── Caddyfile                 # реверс-прокси: TLS, лимит тела, flush для SSE
├── README.md                     # подключение к DSH/Cursor, curl-примеры
├── docs/
│   ├── ARCHITECTURE.md           # этот файл
│   ├── api-reference.md          # сводная справка по маршрутам
│   └── api-extract.txt           # извлечённый текст PDF
├── src/kandinsky_mcp/
│   ├── __main__.py               # выбор транспорта
│   ├── config.py                 # pydantic-settings
│   ├── server.py                 # MCPServer + сборка приложения
│   ├── auth.py                   # X-API-Key, rate limit, подпись ссылок
│   ├── http_app.py               # /mcp /files/* /healthz /readyz /metrics
│   ├── api/
│   │   ├── client.py             # httpx, Bearer, ретраи, 429
│   │   ├── routes.py             # реестр + сверка с openapi.json
│   │   └── errors.py             # маппинг кодов API → доменные ошибки
│   ├── domain/
│   │   ├── tasks.py              # TaskManager, поллеры, recovery
│   │   ├── store.py              # /data, атомарная запись, TTL
│   │   └── media.py              # Pillow: валидация/нормализация
│   └── tools/
│       ├── image.py   tasks.py   info.py
└── tests/
    ├── test_routes.py  test_errors.py  test_store.py  test_tools.py
    └── integration_live.py       # реальный токен, опционально
```

---

## 6. План по этапам

| Этап | Результат | Проверка |
|---|---|---|
| 0 | Скелет: `uv`, `ruff`, `pytest`, структура пакета | линт и пустые тесты проходят |
| 1 | **Проверка SDK**: `MCPServer` + `streamable-http` + `X-API-Key` + кастомный роут; выбор v2/v1 | запрос к `/mcp` через curl / MCP Inspector |
| 2 | `KandinskyClient`: create/status/result, Bearer, ретраи, маппинг ошибок | юнит-тесты на respx |
| 3 | Реестр маршрутов + сверка с `openapi.json` + `kandinsky_routes` | снапшот спеки |
| 4 | `FileStore` + `TaskManager` + поллеры + recovery | тесты: done, failed, 429, «already delivered», рестарт |
| 5 | Тулы `generate` / `edit` / `upscale` + inline/URL | pytest + живой вызов k6 |
| 6 | `task_status` / `task_result` / `tasks_list`, TTL-GC, rate limit, healthz | pytest |
| 7 | Dockerfile + compose + Caddyfile + README + профиль DSH | сборка на VPS, `curl` до `/mcp` |
| 8 | Живой прогон из DSH: правка `cordis.patch.yml`, сгенерированная картинка в чате | скриншот |
| 9 (итер. 2) | Видео, видео+аудио, аватар | по готовности |

---

## 7. Риски

| Риск | Влияние | Митигация |
|---|---|---|
| MCP SDK v2 свежий, ASGI-интеграция | может не лечь на кастомные роуты | этап 1 — проверка до остального кода; откат на 1.30.x |
| Docker локально нет | нельзя собрать образ здесь | собираем на VPS; локально — тесты на bundled Python |
| Одноразовый результат (10 мин / 60 с) | потеря генерации | фоновый поллер + немедленное скачивание (2.1) |
| Расхождение хостов API | 404 при смене хоста | сверка реестра с `openapi.json` при старте |
| Строгий цензор по умолчанию `true` | 422 на безобидных промптах | явный параметр `censor`, код `censored`, подсказка «перепиши промпт» |
| Лимит 2048 символов | 422 на длинных промптах | валидация до запроса |
| `additionalProperties:false` в params | 422 на лишний ключ | собирать `params` из белого списка маршрута |
| Квоты (день/месяц) | 429 посреди работы | локальный счётчик, `quota_*` коды, rate limit |
| Опечатка `aspect_ration` в API | 422 при `aspect_ratio` | принимать оба, наружу — как в схеме |

---

## 8. Что осталось уточнить

Блокирующие вопросы Q1–Q6 закрыты (§0). Ниже — уточнения; если промолчите,
подставлю указанные умолчания и двинусь дальше.

| № | Вопрос | Умолчание |
|---|---|---|
| Q7 | `censor` по умолчанию: `true` (безопасно, но ждёт воркер-цензор) или `false` (быстрее)? | `true` |
| Q8 | Нужен ли `kandinsky_tasks_list` агенту? | да, с `limit=20` |
| Q9 | Срок хранения результатов | 7 дней |
| Q10 | Приём входных картинок по https-URL | нет, только путь и base64 |
| Q11 | Один общий `KANDINSKY_TOKEN` или pass-through токена клиента | один общий из env |
| Q12 | Есть ли доступ к VPS из этой сессии (SSH/деплой) или образ вы выкладываете сами? | вы выкладываете сами, я готовлю всё для `docker compose up` |
| Q13 | Язык описаний тулов | двуязычные RU/EN |

Отдельно: **токен никуда не присылайте в чат** — он нужен только в `.env` на VPS.
Для живого прогона (этапы 5, 8) достаточно, чтобы вы сами прогнали один вызов и
показали ответ, либо положили токен в файл `.env`, который я не читаю и не логирую.
