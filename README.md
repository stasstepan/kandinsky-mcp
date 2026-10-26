# Kandinsky MCP

MCP-сервер для генерации изображений через [Kandinsky API](https://kandinskyapi.sberdevices.ru/docs).
Работает как инструмент агента: Cursor, Windsurf, DeepSeek Harness и любой другой MCP-клиент.

Реализован под **новый асинхронный API** (`/tasks/...`, `Authorization: Bearer`), а не под
старый FusionBrain (`api-key.fusionbrain.ai`, `X-Key`/`X-Secret`). Подробности о различиях —
в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), справка по маршрутам — в
[docs/api-reference.md](docs/api-reference.md).

## Возможности

- Генерация по тексту (Kandinsky 6 и 5), редактирование по картинке, увеличение разрешения.
- **Результат не теряется.** API отдаёт файл однократно и хранит его 10 минут; сервер
  скачивает результат фоновым поллером сразу, как только задача готова, и хранит у себя.
  Повторный вызов инструмента не «сжигает» уже полученную картинку.
- Картинка возвращается **превью прямо в ответе** плюс подписанной ссылкой на файл.
- Два транспорта в одном образе: Streamable HTTP (`/mcp`) и stdio.
- Ключ доступа `X-API-Key`, ограничение частоты запросов, non-root контейнер.

## Инструменты

| Инструмент | Что делает |
|---|---|
| `kandinsky_image_generate` | Изображение по тексту. `model=k6\|k5`, `resolution`, `beautificator`, `censor` |
| `kandinsky_image_edit` | Изменение картинки по описанию (1–3 входных изображения), `model=k6\|kandishop` |
| `kandinsky_image_upscale` | Увеличение ×2 или ×4 (super resolution), `one_step_t` |
| `kandinsky_task_status` | Состояние задачи (`queued`/`processing`/`saved`/`failed`), `refresh=true` — спросить API |
| `kandinsky_task_result` | Готовый файл: ссылка, размер, sha256, превью. В API не ходит |
| `kandinsky_tasks_list` | Последние задачи с фильтрами |
| `kandinsky_routes` | Справка: какие модели доступны, какие параметры и значения допустимы |

Ответ инструментов всегда одного вида:

```json
{
  "task_id": "1842",
  "status": "saved",
  "model": "k6-image-t2i",
  "prompt": "закат над морем",
  "params": { "query": "закат над морем", "resolution": "1024x1024" },
  "elapsed_ms": 18400,
  "media": [{
    "mime": "image/png",
    "bytes": 1482311,
    "sha256": "9f2c…",
    "url": "https://kandinsky.example/files/results/2026-02-14/1842.png?exp=…&sig=…",
    "path": "results/2026-02-14/1842.png"
  }],
  "next": "Файл сохранён. Ссылка media[].url отдаёт его по подписи."
}
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

### Подключение к агенту

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
        toolCallTimeoutMs: 150000   # генерация с ожиданием занимает до минуты
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

`toolCallTimeoutMs` важен: по умолчанию инструмент ждёт готовности до 60 секунд
(`wait_seconds`). Если поставить меньше — вызов вернёт `task_id`, а результат
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
| `RATE_LIMIT_PER_MINUTE` | нет | `30` | защита дневной квоты API |
| `ROUTE_SYNC` | нет | `true` | сверять маршруты с `openapi.json` |
| `ALLOW_LOCAL_PATHS` | нет | `false` | читать входные картинки с диска |

## Caddy

`deploy/Caddyfile` — готовый конфиг. Две строки в нём принципиальны:

- `flush_interval -1` — без этого Caddy буферизует SSE и MCP-клиент не получает события;
- `request_body { max_size 40MB }` — base64 раздувает картинку на треть.

Если Caddy в контейнере, задайте `UPSTREAM=kandinsky-mcp:8000` (в compose уже задано).

## Эндпоинты

| Путь | Доступ | Назначение |
|---|---|---|
| `POST /mcp` | `X-API-Key` | MCP Streamable HTTP |
| `GET /files/<path>?exp=&sig=` | подпись в URL | готовые изображения |
| `GET /healthz` | публичный | живость процесса |
| `GET /readyz` | публичный | доступность Kandinsky API (кеш 30 с) |

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
