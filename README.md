# Kandinsky MCP

MCP-сервер для генерации изображений через [Kandinsky API](https://kandinskyapi.sberdevices.ru/docs).
Работает как инструмент агента: GigaCowork, Cursor, DeepSeek Harness и любой другой MCP-клиент.

API асинхронный: заявка создаётся, затем опрашивается статус и скачивается результат.
Сервер берёт эту механику на себя — агенту остаётся один вызов инструмента.
Устройство описано в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), справка
по маршрутам — в [docs/api-reference.md](docs/api-reference.md).

## Возможности

- Генерация по тексту (Kandinsky 6 и 5), редактирование по картинке, увеличение разрешения.
- **Результат не теряется.** API отдаёт файл однократно и хранит его 10 минут; сервер
  скачивает результат фоновым поллером сразу, как только задача готова, и хранит у себя.
  Повторный вызов инструмента не «сжигает» уже полученную картинку.
- **Файлы сессии GigaCowork:** вход — `read_url` в edit/upscale, выход —
  `x-session-file-output: url`.
- Картинка возвращается **превью в ответе** (GigaCowork показывает её в чате),
  а ссылка `download_url` дублируется в тексте, чтобы агент мог отдать её
  пользователю. Метаданные — в `structuredContent`.
- Два транспорта в одном образе: Streamable HTTP (`/mcp`) и stdio.
- Ключ доступа `X-API-Key`, ограничение частоты запросов, non-root контейнер.

## Инструменты

| Инструмент | Что делает |
|---|---|
| `kandinsky_image_generate` | Изображение по тексту. `model=k6\|k5`, `resolution`, `beautificator`, `censor` |
| `kandinsky_image_edit` | Изменение картинки по описанию (1–3 входных изображения или файл сессии `read_url`), `model=k6\|kandishop` |
| `kandinsky_image_upscale` | Увеличение ×2 или ×4 (super resolution), `creativity` |
| `kandinsky_task_status` | Состояние задачи (`queued`/`processing`/`saved`/`failed`), `refresh=true` — спросить API |
| `kandinsky_task_result` | Готовый файл: метаданные, `download_url`, превью. В API не ходит |
| `kandinsky_tasks_list` | Последние задачи с фильтрами |
| `kandinsky_routes` | Справка: какие модели доступны, какие параметры и значения допустимы |

Текст ответа — короткая сводка: `task_id`, статус, метаданные файла и
`download_url`. Машинные данные (включая `filename`, `content_type`, `size`)
лежат в `structuredContent`; GigaCowork по аннотации `x-session-file-output`
забирает файл в сессию, а ссылка в тексте остаётся fallback.

```json
{
  "task_id": "1842",
  "status": "saved",
  "model": "k6-image-t2i",
  "prompt": "закат над морем",
  "params": { "query": "закат над морем", "resolution": "1024x1024" },
  "elapsed_ms": 18400,
  "filename": "1842.jpg",
  "content_type": "image/jpeg",
  "download_url": "https://kandinsky.example/files/results/2026-02-14/1842.jpg?exp=…&sig=…",
  "size": 148231,
  "media": [{
    "filename": "1842.jpg",
    "mime": "image/jpeg",
    "bytes": 148231,
    "sha256": "9f2c…",
    "url": "https://kandinsky.example/files/results/2026-02-14/1842.jpg?exp=…&sig=…",
    "path": "results/2026-02-14/1842.jpg"
  }],
  "next": "Файл готов: filename, размер и download_url — в ответе; ссылка живёт 7 дней."
}
```

Текстовая часть того же ответа:

```
task_id=1842, status=saved, model=k6-image-t2i; файл: 1842.jpg, image/jpeg, 148231 Б, sha256=9f2c…
download_url: https://kandinsky.example/files/results/2026-02-14/1842.jpg?exp=…&sig=…
```

## Быстрый старт

### Локально (stdio, без Docker)

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install .
export KANDINSKY_TOKEN=...                        # токен Kandinsky API
MCP_TRANSPORT=stdio python -m kandinsky_mcp
```

В stdio-режиме сервер и агент на одной машине, поэтому входные картинки можно передавать
путём к файлу, а не только base64.

### Docker

```bash
cp .env.example .env      # заполнить KANDINSKY_TOKEN и MCP_API_KEY
docker compose up -d --build
curl -s localhost:8000/healthz
```

Порт наружу в compose не публикуется — предполагается, что снаружи стоит Caddy.
Для локальной проверки раскомментируйте `ports` в `docker-compose.yml`.

### Подключение к GigaCowork

Основной сценарий — GigaCowork ([cowork.ru](https://cowork.ru)): коннектор типа
`Streamable HTTP`, адрес `https://<ваш-домен>/mcp`, авторизация «HTTP-заголовки»,
заголовок `X-API-Key` со значением ключа. Для общей витрины готовы
[Definition](gigacowork/definition.yaml) и [карточка](gigacowork/integration.yaml);
полный чек-лист и ограничения — в [docs/GIGACOWORK.md](docs/GIGACOWORK.md).

Профиль GigaCowork сервер соблюдает сам:

- `initialize` согласует MCP `2025-11-25`, `notifications/initialized` → 202;
- имена инструментов `[A-Za-z0-9_-]{1,64}`, полный каталог в первом `tools/list`;
- неизвестный инструмент и невалидные аргументы → JSON-RPC `-32602`,
  прикладная ошибка → `isError: true`;
- `GET /mcp` без сессии → `405`, чужой `Origin` → `403`
  (доверенные задаются `MCP_ALLOWED_ORIGINS`);
- файлы сессии: вход через `read_url` (`x-session-file-input`), выход —
  `x-session-file-output: url`; GigaCowork забирает файл по `download_url` и
  кладёт в сессию (проверено: после генерации платформа делает GET `/files/...`).
  Ссылка дублируется в тексте как fallback для версий без файлового режима;
- inline-превью не режется по умолчанию (`MAX_INLINE_RESULT_CHARS=0`): только
  из него GigaCowork показывает картинку в чате;
- `download_url` отдаётся без сжатия (`Content-Encoding: identity`).

Проверка совместимости без траты квоты:

```bash
MCP_URL=https://<ваш-домен>/mcp MCP_API_KEY=... python scripts/compat_check.py
```

DeepSeek Harness — `~/.dsh/profiles/desktop/cordis.patch.yml`:

```yaml
- insert:
    - id: mcp-kandinsky
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: kandinsky
        transport: streamable-http
        url: https://kandinsky.example.com/mcp
        headers:
          X-API-Key: <MCP_API_KEY>
        toolCallTimeoutMs: 90000   # сервер ждёт результат не дольше 45 с
        failOnStartupError: false
```

Cursor / Windsurf — тот же блок в `mcpServers`, для stdio:

```json
{
  "mcpServers": {
    "kandinsky": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "-e", "MCP_TRANSPORT=stdio",
               "-e", "KANDINSKY_TOKEN=${KANDINSKY_TOKEN}", "kandinsky-mcp:latest"]
    }
  }
}
```

`toolCallTimeoutMs` важен: сервер отвечает в пределах бюджета вызова
(`CALL_BUDGET_SECONDS=45`), а если результат не готов — возвращает `task_id`.
Если поставить таймаут меньше 45 с, клиент оборвёт вызов раньше ответа; результат
можно забрать позже через `kandinsky_task_result` (файл уже будет на сервере).

## Настройка

Все параметры — через переменные окружения, полный список в [.env.example](.env.example).

| Переменная | Обязательна | По умолчанию | Смысл |
|---|---|---|---|
| `KANDINSKY_TOKEN` | да | — | Bearer-токен Kandinsky API |
| `MCP_API_KEY` | для http | — | ключ доступа агента (`X-API-Key`) |
| `FILE_SECRET` | нет | = `MCP_API_KEY` | секрет подписи ссылок на файлы |
| `PUBLIC_BASE_URL` | для http | `http://127.0.0.1:8000` | внешний адрес для ссылок |
| `MCP_TRANSPORT` | нет | `http` | `http` или `stdio` |
| `DATA_DIR` | нет | `/data` | где лежат задачи и результаты |
| `RESULT_TTL_HOURS` | нет | `168` | срок хранения файлов |
| `DEFAULT_CENSOR` | нет | `true` | цензура контента по умолчанию |
| `DEFAULT_WAIT_SECONDS` | нет | `25` | ожидание результата внутри вызова (ограничивается бюджетом вызова) |
| `CALL_BUDGET_SECONDS` | нет | `45` | общий бюджет `tools/call`: создание + ожидание |
| `RATE_LIMIT_PER_MINUTE` | нет | `120` | защита квоты API от циклов |
| `ROUTE_SYNC` | нет | `true` | сверять маршруты с `openapi.json` |
| `ALLOW_LOCAL_PATHS` | нет | `false` | читать входные картинки с диска |
| `MAX_INLINE_RESULT_CHARS` | нет | `0` | бюджет inline-превью; `0` — без ограничения |
| `MCP_ALLOWED_ORIGINS` | нет | — | доверенные `Origin` для `/mcp` (через запятую; `PUBLIC_BASE_URL` доверяется автоматически) |
| `SESSION_FILE_MAX_BYTES` | нет | `104857600` | лимит входного файла сессии |
| `SESSION_FILE_TIMEOUT` | нет | `120` | таймаут скачивания файла сессии, с |

## Caddy

`deploy/Caddyfile` — готовый конфиг. Принципиальные строки:

- `flush_interval -1` — без этого Caddy буферизует SSE и MCP-клиент не получает события;
- `request_body { max_size 40MB }` — base64 раздувает картинку на треть;
- `/files/*` исключён из `encode` и `log_skip`: `download_url` должен отдаваться
  без сжатия (`Content-Encoding: identity`), а подписанные query-параметры
  не должны попадать в access-логи.

Если Caddy в контейнере, задайте `UPSTREAM=kandinsky-mcp:8000` (в compose уже задано).

## Эндпоинты

| Путь | Доступ | Назначение |
|---|---|---|
| `POST /mcp` | `X-API-Key` | MCP Streamable HTTP |
| `GET /mcp` | — | `405` без сессии (серверных уведомлений нет) |
| `GET /files/<path>?exp=&sig=` | подпись в URL | готовые изображения без сжатия |
| `GET /healthz` | публичный | живость процесса |
| `GET /readyz` | публичный | доступность Kandinsky API (кеш 30 с) |
| `GET /stats` | `X-API-Key` | расход квоты по маршрутам за месяц |

## Разработка

```bash
pip install -e ".[dev]"
pytest -q
ruff check src tests
```

Тесты не ходят в сеть: клиент API проверяется на `respx`, фоновый поллер — на
скриптованном фейке.

Живая проверка (одна настоящая генерация, тратит квоту):

```bash
# в контейнере — самый простой способ убедиться, что всё работает
docker compose exec kandinsky-mcp kandinsky-mcp-smoke

# локально
KANDINSKY_TOKEN=... python -m kandinsky_mcp.smoke "кот в шляпе" 1280x768
```

Скрипт проходит весь путь: сверка реестра с `openapi.json`, создание задачи,
поллинг, однократное скачивание, проверка файла на диске.

## Ловушки API, которые сервер берёт на себя

| Проблема | Что делает сервер |
|---|---|
| Результат одноразовый: 10 минут на скачивание, потом удаление | фоновый поллер скачивает сразу; `kandinsky_task_result` читает с диска |
| В PDF разрешение `1280:768`, а схема требует `1280x768` | нормализует любой разделитель (`:`, `x`, `×`) |
| Ключ называется `aspect_ration` (опечатка в API) | принимает и `aspect_ratio`, наружу отправляет как в схеме |
| `additionalProperties: false` → лишний ключ даёт 422 | собирает `params` по белому списку маршрута |
| Лимит промпта 2048 символов | проверяет до запроса, не тратя квоту |
| Входная картинка ≤ 25 МБ и ≤ 2048 px (superres ≤ 4 МБ / 1024 px) | уменьшает автоматически и сообщает об этом в `warnings` |
| 429 с `LIMIT_EXHAUSTED` четырёх видов | различает `quota_daily`/`monthly`/`concurrent`/`rate` и объясняет, что делать |
| Квоты не тратятся на чтение статуса | опрос бесплатный, поэтому поллинг агрессивный и безопасный |
| Заголовок врёт: `Content-Type: image/png`, а байты — JPEG (проверено живым прогоном) | определяет формат по сигнатуре файла (`\xff\xd8\xff` и т.п.) и сохраняет с правильным расширением |

## Безопасность

- Токен Kandinsky живёт только в окружении; в логи и ответы не попадает.
- `/mcp` требует `X-API-Key` (или `Authorization: Bearer`). Без ключа сервер не стартует
  в HTTP-режиме — это осознанно, а не недосмотр.
- Ссылки на файлы подписаны и живут ограниченное время; каталог данных не отдаётся целиком.
- Контейнер: non-root uid 10001, `read_only` rootfs, `cap_drop: ALL`, `no-new-privileges`,
  единственный writable путь — том `/data`.
- В HTTP-режиме вход по локальному пути запрещён: иначе ключ доступа превращался бы
  в право прочитать любой файл сервера.

## Лицензия

MIT.
