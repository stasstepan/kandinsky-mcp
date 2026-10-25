"""HTTP-клиент Kandinsky API. Знает только про REST, ничего про MCP.

Отдельный слой, чтобы тестировать работу с API без поднятия MCP-сервера
и чтобы тулы не зависели от httpx напрямую.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .errors import KandinskyError, classify, network_error

log = logging.getLogger("kandinsky_mcp.api")

# Гейтвейные коды: запрос с высокой вероятностью не был обработан.
_RETRY_ON_STATUS = frozenset({502, 503, 504})
_GATEWAY_STATUS = frozenset({502, 503, 504})

_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "audio/wav": ".wav",
    "audio/mpeg": ".mp3",
}


@dataclass(slots=True)
class TaskStatus:
    status: str
    error: str | None = None
    http_status: int = 200

    TERMINAL = frozenset({"done", "failed"})

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL

    @property
    def is_done(self) -> bool:
        return self.status == "done"

    @property
    def is_failed(self) -> bool:
        return self.status == "failed"


@dataclass(slots=True)
class DownloadedFile:
    content: bytes
    content_type: str
    content_disposition: str = ""

    @property
    def extension(self) -> str:
        mime = self.content_type.split(";")[0].strip().lower()
        return _EXT_BY_MIME.get(mime, ".bin")

    @property
    def size(self) -> int:
        return len(self.content)


def _strip_params(content_type: str) -> str:
    return content_type.split(";")[0].strip().lower()


def sniff_mime(content: bytes) -> str | None:
    """Определяет формат по сигнатуре файла.

    Нужно потому, что API присылает JPEG с заголовком `Content-Type: image/png`
    (проверено живым прогоном). Верить заголовку нельзя: файл получил бы
    расширение .png, а клиент, доверяющий расширению, отказался бы его рисовать.
    """
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content[:4] == b"RIFF" and content[8:12] == b"WAVE":
        return "audio/wav"
    if content[4:8] == b"ftyp":
        return "video/mp4"
    return None


class KandinskyClient:
    """Асинхронный клиент. Один экземпляр на процесс, закрывается в lifespan."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        download_timeout: float = 120.0,
        max_attempts: int = 3,
        user_agent: str = "kandinsky-mcp/0.1",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.download_timeout = download_timeout
        self.max_attempts = max_attempts
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": user_agent,
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(timeout, connect=10.0),
            transport=transport,
            follow_redirects=False,
        )

    async def __aenter__(self) -> KandinskyClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # --- низкий уровень ---------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        attempts: int | None = None,
        timeout: float | None = None,
        allow_status: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        """Запрос с ретраями.

        Повторяем только то, что безопасно или почти наверняка не было обработано:
        сетевые ошибки, 502/503/504. На остальных кодах сразу поднимаем доменную
        ошибку, чтобы не плодить дубликаты задач и не жечь квоту.
        """
        total = attempts or self.max_attempts
        last: KandinskyError | None = None
        for attempt in range(1, total + 1):
            try:
                response = await self._client.request(
                    method,
                    url,
                    json=json,
                    timeout=timeout or self.timeout,
                )
            except httpx.HTTPError as exc:
                last = network_error(exc)
                if attempt == total:
                    raise last from exc
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
                continue

            if response.status_code in allow_status:
                return response

            if response.status_code in _GATEWAY_STATUS and attempt < total:
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
                continue

            raise self._to_error(response)

        raise last or KandinskyError("upstream_error", "Запрос не выполнен")

    @staticmethod
    def _to_error(response: httpx.Response) -> KandinskyError:
        payload: Any = None
        text = ""
        try:
            text = response.text
            payload = response.json()
        except Exception:
            payload = text[:500]
        return classify(response.status_code, payload, raw_text=text)

    # --- операции ---------------------------------------------------------

    async def health(self) -> bool:
        """GET /health. Не бросает: используется в /readyz."""
        try:
            response = await self._client.get("/health", timeout=5.0)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def fetch_openapi(self) -> dict[str, Any]:
        response = await self._request("GET", "/openapi.json")
        return response.json()

    async def create_task(
        self,
        route: str,
        params: dict[str, Any],
        *,
        censor: bool | None = None,
    ) -> str:
        """POST /tasks/<route> → task_id.

        `censor=None` означает «не передавать»: сервер применит своё умолчание.
        """
        body: dict[str, Any] = {"params": params}
        if censor is not None:
            body["censor"] = censor
        response = await self._request("POST", f"/tasks/{route}", json=body)
        data = response.json()
        task_id = str(data.get("task_id", "")).strip()
        if not task_id:
            raise KandinskyError(
                "upstream_error",
                f"API не вернул task_id: {str(data)[:200]}",
                response.status_code,
            )
        return task_id

    async def get_status(self, task_id: str) -> TaskStatus:
        """GET /tasks/{id}. 202 = в работе, 200 = терминальный статус."""
        response = await self._request("GET", f"/tasks/{task_id}", allow_status=(200, 202))
        data = response.json()
        status = str(data.get("status", "")).strip().lower()
        if not status:
            raise KandinskyError(
                "upstream_error",
                f"API не вернул status: {str(data)[:200]}",
                response.status_code,
            )
        error = data.get("error")
        return TaskStatus(
            status=status,
            error=str(error) if error else None,
            http_status=response.status_code,
        )

    async def download_result(self, task_id: str) -> DownloadedFile:
        """GET /tasks/{id}/result.

        Внимание: выдача одноразовая. После успешного скачивания медиа живёт
        ещё ~60 секунд, повторный запрос вернёт 422. Вызывать ровно один раз.
        """
        response = await self._request(
            "GET",
            f"/tasks/{task_id}/result",
            timeout=self.download_timeout,
        )
        header_mime = _strip_params(response.headers.get("content-type", "application/octet-stream"))
        sniffed = sniff_mime(response.content)
        content_type = sniffed or header_mime
        if sniffed and header_mime != sniffed and not header_mime.startswith("application/"):
            log.warning(
                "API заявил %s, а по байтам %s (task_id=%s) — используем определённый формат",
                header_mime,
                sniffed,
                task_id,
            )
        return DownloadedFile(
            content=response.content,
            content_type=content_type,
            content_disposition=response.headers.get("content-disposition", ""),
        )
