# Как развернуть

Пошагово, от пустой папки до работающего инструмента в агенте.
Всё выполняется на сервере, локально собирать ничего не нужно.

## 0. Что должно быть на сервере

```bash
docker --version           # 20.10+ (лучше 24+)
docker compose version     # v2 (плагин). Если есть только docker-compose — команды ниже заменить
```

Если Docker нет — официальный способ:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # после этого перелогиниться
```

## 1. Скопировать проект

С машины, где лежит `kandinsky-mcp.tar.gz` (70 КБ, без `.venv` и мусора):

```bash
scp kandinsky-mcp.tar.gz user@server:/opt/
```

На сервере:

```bash
cd /opt
tar -xzf kandinsky-mcp.tar.gz
cd kandinsky-mcp
```

Альтернатива без scp — залить в git и сделать `git clone`. Внутри уже есть
`.gitignore`, так что `.env` и данные не попадут в репозиторий.

## 2. Настроить окружение

```bash
cp .env.example .env
```

Обязательно заполнить два поля:

```bash
# токен Kandinsky API
KANDINSKY_TOKEN=<токен из личного кабинета>
# ключ, который агент будет присылать в X-API-Key
MCP_API_KEY=$(openssl rand -hex 32)
```

И проверить внешний адрес — из него строятся ссылки на файлы:

```bash
PUBLIC_BASE_URL=https://kandinsky.example.com
```

Права на файл с секретами:

```bash
chmod 600 .env
```

## 3. Собрать и запустить

```bash
docker compose up -d --build
```

Первый запуск занимает пару минут (скачивание базового образа и зависимостей).
Дальше:

```bash
docker compose logs -f kandinsky-mcp
```

В логе должно быть:

```
запуск HTTP на 0.0.0.0:8000/mcp (API: https://kandinskyapi.sberdevices.ru)
HTTP Request: GET https://kandinskyapi.sberdevices.ru/openapi.json "HTTP/1.1 200 OK"
реестр маршрутов совпадает с API        # или перечень отсутствующих маршрутов
Uvicorn running on http://0.0.0.0:8000
```

Строка про сверку реестра — главный признак, что сервис достучался до API.
Если увидите `не удалось получить openapi.json` — у контейнера нет выхода в сеть.

## 4. Проверить, что работает

```bash
# процесс жив
curl -s localhost:8000/healthz
# {"status":"ok","version":"0.1.0","transport":"http"}

# API Kandinsky доступен из контейнера
curl -s localhost:8000/readyz
# {"status":"ok","kandinsky_api":"reachable",...}

# одна настоящая генерация: полный путь, включая скачивание файла
docker compose exec kandinsky-mcp kandinsky-mcp-smoke
```

`kandinsky-mcp-smoke` — это и есть проверка «всё действительно работает»:
он создаёт задачу, поллит её, скачивает результат и печатает путь к файлу.
Тратит одну генерацию из квоты.

Пример успешного вывода:

```
task_id=524, ждём результат…
итог: status=saved за 81.3 с, опросов=3
  файл:    /data/results/2026-10-22/524.jpg
  формат:  image/jpeg (131363 байт)
  sha256:  d19892a9...
```

## 5. Подключить домен

Дальше зависит от того, есть ли на сервере уже Caddy.

### Вариант A — Caddy уже стоит (он обслуживает fs/postgres/playwright)

Ничего в compose не меняем: сервис уже опубликован на `127.0.0.1:8000`,
системный Caddy его увидит. Контейнерный Caddy не запускаем — он занял бы
порты 80/443.

Добавить в существующий Caddyfile блок из `deploy/Caddyfile` (только сам блок
`kandinsky.example.com { ... }`, без комментариев про `UPSTREAM`):

```caddyfile
kandinsky.example.com {
    encode zstd gzip
    request_body { max_size 40MB }
    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1
        transport http {
            read_timeout 0
            write_timeout 0
        }
    }
}
```

Перезагрузить:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Две строки в этом блоке не декоративные: `flush_interval -1` отключает
буферизацию SSE (без неё MCP-клиент висит без событий), а `max_size 40MB`
нужен, потому что base64 раздувает картинку на треть.

### Вариант B — Caddy ещё не установлен

Тогда пусть Caddy живёт в compose:

```bash
docker compose --profile edge up -d
```

Он сам получит сертификат для домена из `deploy/Caddyfile`
(проверьте, что A-запись домена указывает на сервер, порты 80/443 открыты).

## 6. Подключить к агенту

В `~/.dsh/profiles/desktop/cordis.patch.yml`:

```yaml
- insert:
    - id: mcp-kandinsky
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: kandinsky
        transport: streamable-http
        url: https://kandinsky.example.com/mcp
        headers:
          X-API-Key: <MCP_API_KEY из .env>
        toolCallTimeoutMs: 150000
        failOnStartupError: false
```

`toolCallTimeoutMs` важен: инструмент ждёт готовности картинки до 60 секунд
(`wait_seconds`), плюс запас на сеть. Если поставить меньше — вызов вернёт
`task_id`, файл всё равно скачается, но забрать его придётся через
`kandinsky_task_result`.

Перезапустить DSH, затем в чате: «сгенерируй кота в стиле акварели».

## 7. Обслуживание

```bash
docker compose pull           # если образ обновился
docker compose up -d --build  # пересобрать после изменения кода
docker compose logs -f        # логи
docker compose down           # остановить (том с картинками сохраняется)
```

Данные лежат в томе `kandinsky-data`: `/data/tasks/*.json` и `/data/results/<дата>/`.
Срок хранения — `RESULT_TTL_HOURS` (по умолчанию 7 дней), чистка раз в час.

## Если что-то не работает

| Симптом | Причина и что делать |
|---|---|
| `bind: address already in use` на 80/443 | на хосте уже есть веб-сервер. Не запускайте профиль `edge`, используйте вариант A |
| `readyz` отдаёт 503, `degraded` | из контейнера нет доступа к `kandinskyapi.sberdevices.ru`: проверьте DNS и egress |
| В логах `реестр маршрутов…`, но `k6_t2va_lite` в отсутствующих | это нормально: маршрут есть только на `kandinskyapi.ru` |
| Агент получает 401 | `X-API-Key` не совпадает с `MCP_API_KEY` из `.env` |
| Инструмент возвращает `auth_failed` | неверный `KANDINSKY_TOKEN`; проверьте `readyz` и лог старта |
| Инструмент возвращает `censored` | сработал цензор: перепишите промпт или создайте задачу с `censor=false` |
| Картинка не открывается по ссылке | `PUBLIC_BASE_URL` не совпадает с реальным доменом — ссылки подписаны |
| `wait_seconds` истёк, статус `processing` | нормально: вызовите `kandinsky_task_result` с этим `task_id` |
