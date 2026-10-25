# Kandinsky MCP — образ для деплоя на VPS.
#
# Сборка:  docker build -t kandinsky-mcp:latest .
# Запуск:  docker compose up -d
#
# Стек собран из колёс (Pillow, pydantic, uvicorn публикуют win/manylinux-колёса),
# поэтому компиляторы в финальный образ не нужны: одна стадия, slim-база.

FROM python:3.12-slim

# ca-certificates обязателен: httpx (через httpx2/truststore) проверяет TLS
# по системному хранилищу, а не по встроенному бандлу сертификатов.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

# Non-root пользователь с фиксированным uid: права на /data не «прыгают».
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

# Сначала только метаданные зависимостей — слой кешируется между сборками кода.
COPY pyproject.toml README.md ./
RUN python -m pip install --no-cache-dir --upgrade pip

COPY src ./src
RUN python -m pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=8000 \
    DATA_DIR=/data

RUN mkdir -p /data && chown -R app:app /data /app

USER app
VOLUME ["/data"]
EXPOSE 8000

# /healthz публичный и не ходит в внешний API — годится для liveness.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=4); sys.exit(0 if r.status==200 else 1)"

# Режим выбирается MCP_TRANSPORT: http (uvicorn) или stdio (для локальной отладки).
CMD ["python", "-m", "kandinsky_mcp"]
