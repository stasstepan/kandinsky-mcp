"""Маппинг ошибок API в доменные коды — самая «дорогая» логика для агента."""

from __future__ import annotations

import pytest

from kandinsky_mcp.api import errors as e


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (401, {"detail": "Not authenticated"}, e.AUTH_FAILED),
        (403, {"detail": "Forbidden"}, e.AUTH_FAILED),
        (404, {"detail": "Task not found"}, e.NOT_FOUND),
        (400, {"detail": "task_id must be numeric"}, e.BAD_REQUEST),
        (413, {"detail": "too large file"}, e.INPUT_TOO_LARGE),
    ],
)
def test_status_mapping(status: int, payload: dict, expected: str) -> None:
    assert e.classify(status, payload).code == expected


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("text blocked by censor: unsafe content", e.CENSORED),
        ("input image blocked by censor: nudity", e.CENSORED),
        ("output blocked by censor: unsafe content", e.CENSORED),
        ("censor service unavailable: GigaChat returned no response", e.CENSOR_UNAVAILABLE),
        ("prompt too long 3000chars>2048 max chars limit", e.PROMPT_TOO_LONG),
        ("too big resolution", e.INPUT_BAD_RESOLUTION),
        ("too large file", e.INPUT_TOO_LARGE),
        ("Task is not ready (status=processing)", e.NOT_READY),
        (
            "Task result was already delivered to the client. Repeated download is not available",
            e.RESULT_GONE,
        ),
    ],
)
def test_422_details(detail: str, expected: str) -> None:
    assert e.classify(422, {"detail": detail}).code == expected


def test_censor_unavailable_is_checked_before_censored() -> None:
    """Строка про недоступность цензора содержит слово censor — порядок веток важен."""
    payload = {"detail": "output blocked by censor unavailable: GigaChat returned no response"}
    assert e.classify(422, payload).code == e.CENSOR_UNAVAILABLE


@pytest.mark.parametrize(
    ("qtype", "expected"),
    [
        ("daily", e.QUOTA_DAILY),
        ("monthly", e.QUOTA_MONTHLY),
        ("concurrent", e.QUOTA_CONCURRENT),
        ("rate", e.QUOTA_RATE),
        ("unknown-kind", e.QUOTA_RATE),
    ],
)
def test_429_quota_types(qtype: str, expected: str) -> None:
    payload = {"detail": {"code": "LIMIT_EXHAUSTED", "type": qtype}}
    assert e.classify(429, payload).code == expected


def test_429_without_structured_detail_falls_back_to_rate() -> None:
    assert e.classify(429, {"detail": "slow down"}).code == e.QUOTA_RATE


def test_validation_error_is_flattened_for_the_model() -> None:
    payload = {
        "detail": [
            {
                "loc": ["body", "params", "resolution"],
                "msg": "Input should be '1024x1024' or '768x1280'",
                "type": "literal_error",
            }
        ]
    }
    err = e.classify(422, payload)
    assert err.code == e.VALIDATION_ERROR
    assert "params.resolution" in err.message
    assert "Input should be" in err.message


def test_5xx_is_upstream_and_retryable() -> None:
    err = e.classify(500, {"detail": "boom"})
    assert err.code == e.UPSTREAM_ERROR
    assert err.retryable is True


def test_censored_is_not_retryable() -> None:
    err = e.classify(422, {"detail": "text blocked by censor: x"})
    assert err.retryable is False
    assert "перепишите промпт" in err.hint.lower()


def test_network_error_is_retryable() -> None:
    err = e.network_error(TimeoutError("timed out"))
    assert err.code == e.NETWORK_ERROR
    assert err.retryable is True


def test_agent_text_contains_code_message_hint_and_status() -> None:
    err = e.classify(422, {"detail": "prompt too long 3000chars>2048 max chars limit"})
    text = err.to_agent_text()
    assert "[prompt_too_long]" in text
    assert "3000chars" in text
    assert "HTTP 422" in text


def test_fatal_and_retryable_sets_do_not_overlap() -> None:
    assert not (e.RETRYABLE & e.FATAL)
