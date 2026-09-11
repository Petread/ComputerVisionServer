from __future__ import annotations

from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_job_id: ContextVar[str | None] = ContextVar("job_id", default=None)


def set_request_context(
    *,
    request_id: str | None = None,
    correlation_id: str | None = None,
    job_id: str | None = None,
) -> None:
    if request_id is not None:
        _request_id.set(request_id)
    if correlation_id is not None:
        _correlation_id.set(correlation_id)
    if job_id is not None:
        _job_id.set(job_id)


def clear_request_context() -> None:
    _request_id.set(None)
    _correlation_id.set(None)
    _job_id.set(None)


def get_request_id() -> str | None:
    return _request_id.get()


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def get_job_id() -> str | None:
    return _job_id.get()

