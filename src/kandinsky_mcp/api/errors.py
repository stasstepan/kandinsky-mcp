"""Ошибки Kandinsky API, переведённые в доменные коды.

Смысл слоя: агент должен получить не «HTTP 422», а код и подсказку, по которой
можно действовать. Модель читает `is_error`-контент и на его основе решает,
переписать промпт, подождать или сдаться.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --- коды, которые видит агент -------------------------------------------------

AUTH_FAILED = "auth_failed"
BAD_REQUEST = "bad_request"
NOT_FOUND = "not_found"
INPUT_TOO_LARGE = "input_too_large"
INPUT_BAD_RESOLUTION = "input_bad_resolution"
PROMPT_TOO_LONG = "prompt_too_long"
CENSORED = "censored"
CENSOR_UNAVAILABLE = "censor_unavailable"
NOT_READY = "not_ready"
RESULT_GONE = "result_gone"
QUOTA_DAILY = "quota_daily"
QUOTA_MONTHLY = "quota_monthly"
QUOTA_CONCURRENT = "quota_concurrent"
QUOTA_RATE = "quota_rate"
VALIDATION_ERROR = "validation_error"
UPSTREAM_ERROR = "upstream_error"
NETWORK_ERROR = "network_error"
NOT_IMPLEMENTED = "not_implemented"

# Что имеет смысл повторять: сеть, 5xx, недоступный цензор, «ещё не готово».
RETRYABLE = frozenset({UPSTREAM_ERROR, NETWORK_ERROR, CENSOR_UNAVAILABLE, NOT_READY})

# Что повторять бессмысленно: сначала надо изменить запрос.
FATAL = frozenset(
    {
        AUTH_FAILED,
        BAD_REQUEST,
        NOT_FOUND,
        INPUT_TOO_LARGE,
        INPUT_BAD_RESOLUTION,
        PROMPT_TOO_LONG,
        CENSORED,
        RESULT_GONE,
        VALIDATION_ERROR,
        NOT_IMPLEMENTED,
    }
)

_QUOTA_BY_TYPE = {
    "daily": QUOTA_DAILY,
    "monthly": QUOTA_MONTHLY,
    "concurrent": QUOTA_CONCURRENT,
    "rate": QUOTA_RATE,
}

_HINTS: dict[str, str] = {
    AUTH_FAILED: "Токен Kandinsky не принят. Проверьте переменную окружения KANDINSKY_TOKEN.",
    BAD_REQUEST: "API отклонил запрос как некорректный.",
    NOT_FOUND: (
        "Задача не существует или принадлежит другому токену. "
        "Проверьте task_id — чужие задачи API не отдаёт (404, а не 403)."
    ),
    INPUT_TOO_LARGE: (
        "Входной файл больше лимита маршрута. Уменьшите разрешение или пережмите "
        "картинку перед отправкой."
    ),
    INPUT_BAD_RESOLUTION: (
        "Сторона входной картинки больше лимита маршрута. Уменьшите изображение "
        "(для superres лимит строже всего: 1024 px по каждой стороне)."
    ),
    PROMPT_TOO_LONG: "Промпт длиннее лимита маршрута. Сократите описание.",
    CENSORED: (
        "Контент заблокирован цензором. Повторять тот же запрос бессмысленно — "
        "перепишите промпт или уберите спорный входной файл."
    ),
    CENSOR_UNAVAILABLE: (
        "Сервис цензуры (GigaChat) недоступен. Запрос не отклонён по существу — "
        "можно повторить позже или создать задачу с censor=false."
    ),
    NOT_READY: "Результат ещё не готов. Дождитесь статуса done и запросите снова.",
    RESULT_GONE: (
        "Результат уже был выдан и удалён: API отдаёт файл однократно и хранит его "
        "10 минут после готовности. Нужна новая генерация — результат не восстановить."
    ),
    QUOTA_DAILY: "Исчерпан дневной лимит запросов на этот маршрут. Дождитесь сброса квоты.",
    QUOTA_MONTHLY: "Исчерпан месячный лимит запросов. Дождитесь сброса квоты.",
    QUOTA_CONCURRENT: (
        "Превышено число параллельных задач (у API их всего две). "
        "Запущенные задачи занимают слоты до 45 минут — дождитесь их завершения, "
        "посмотрите kandinsky_tasks_list, и только потом создавайте новую. "
        "Повторять тот же запрос не нужно: пока задача выполняется, сервер вернёт её же."
    ),
    QUOTA_RATE: "Слишком частые запросы: не выдержан минимальный интервал. Повторите позже.",
    VALIDATION_ERROR: "Параметры не прошли валидацию на стороне API.",
    UPSTREAM_ERROR: "Внутренняя ошибка Kandinsky API. Обычно помогает повтор.",
    NETWORK_ERROR: "Не удалось связаться с Kandinsky API. Проверьте сеть и повторите.",
    NOT_IMPLEMENTED: "Маршрут не поддерживается текущим хостом API.",
}


@dataclass(slots=True)
class KandinskyError(Exception):
    """Доменная ошибка: код для агента + исходный ответ API для логов."""

    code: str
    message: str
    status: int | None = None
    hint: str = ""
    retryable: bool = False

    def __post_init__(self) -> None:
        if not self.hint:
            self.hint = _HINTS.get(self.code, "")
        if not self.retryable:
            self.retryable = self.code in RETRYABLE
        Exception.__init__(self, self.message)

    def to_agent_text(self) -> str:
        parts = [f"[{self.code}] {self.message}"]
        if self.hint:
            parts.append(self.hint)
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "status": self.status,
            "retryable": self.retryable,
        }


def _describe_validation(payload: Any) -> str:
    """FastAPI-подобный 422: detail — список объектов с msg/loc."""
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, list):
        chunks = []
        for item in detail[:3]:
            if isinstance(item, dict):
                loc = ".".join(str(p) for p in item.get("loc", ()) if p != "body")
                msg = item.get("msg", "")
                chunks.append(f"{loc}: {msg}" if loc else str(msg))
        if chunks:
            return "; ".join(chunks)
    return ""


def _detail_text(payload: Any) -> str:
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, dict):
            return str(detail.get("message") or detail.get("code") or "")
    if isinstance(payload, str):
        return payload
    return ""


def classify(
    status: int,
    payload: Any,
    *,
    route: str | None = None,
    raw_text: str = "",
) -> KandinskyError:
    """Переводит ответ API в доменную ошибку. Чистая функция — удобно тестировать."""
    detail = _detail_text(payload)
    lowered = detail.lower()

    if status in (401, 403):
        return KandinskyError(AUTH_FAILED, detail or "Токен не принят", status)

    if status == 404:
        return KandinskyError(NOT_FOUND, detail or "Не найдено", status)

    if status == 400:
        return KandinskyError(BAD_REQUEST, detail or "Некорректный запрос", status)

    if status == 413:
        return KandinskyError(INPUT_TOO_LARGE, detail or "Файл слишком большой", status)

    if status == 429:
        qtype = ""
        if isinstance(payload, dict) and isinstance(payload.get("detail"), dict):
            qtype = str(payload["detail"].get("type", ""))
        code = _QUOTA_BY_TYPE.get(qtype, QUOTA_RATE)
        return KandinskyError(code, detail or "Лимит исчерпан", status)

    if status == 422:
        # Порядок важен: сообщение про недоступность цензора тоже содержит
        # «blocked by censor» в некоторых формулировках, поэтому проверяем
        # недоступность раньше блокировки.
        if "censor" in lowered and "unavailable" in lowered:
            return KandinskyError(CENSOR_UNAVAILABLE, detail, status)
        if "blocked by censor" in lowered:
            return KandinskyError(CENSORED, detail, status)
        if "prompt too long" in lowered:
            return KandinskyError(PROMPT_TOO_LONG, detail, status)
        if "too big resolution" in lowered:
            return KandinskyError(INPUT_BAD_RESOLUTION, detail, status)
        if "too large file" in lowered:
            return KandinskyError(INPUT_TOO_LARGE, detail, status)
        if "already delivered" in lowered or "repeated download" in lowered:
            return KandinskyError(RESULT_GONE, detail, status)
        if "not ready" in lowered:
            return KandinskyError(NOT_READY, detail, status)
        described = _describe_validation(payload) or detail or raw_text[:300]
        return KandinskyError(VALIDATION_ERROR, described or "Ошибка валидации", status)

    if 500 <= status < 600:
        return KandinskyError(UPSTREAM_ERROR, detail or f"Ошибка API: HTTP {status}", status)

    return KandinskyError(
        UPSTREAM_ERROR,
        detail or raw_text[:300] or f"Неожиданный ответ API: HTTP {status}",
        status,
    )


def network_error(exc: BaseException) -> KandinskyError:
    return KandinskyError(NETWORK_ERROR, f"Сеть недоступна: {type(exc).__name__}: {exc}")
