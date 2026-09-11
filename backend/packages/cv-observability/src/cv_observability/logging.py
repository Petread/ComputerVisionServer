from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog

from cv_utils import get_correlation_id, get_job_id, get_request_id


def configure_logging(
    service_name: str, environment: str, *, instance_id: str | None = None
) -> None:
    resolved_instance_id = instance_id or os.getenv("HOSTNAME") or f"{service_name}-{os.getpid()}"
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            structlog.stdlib.add_log_level,
            _inject_required_fields,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        service=service_name,
        env=environment,
        instance_id=resolved_instance_id,
    )


def get_logger(name: str = "cv-platform") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def _inject_required_fields(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    message = event_dict.pop("event", None)
    if message is not None and "message" not in event_dict:
        event_dict["message"] = str(message)
    event_dict.setdefault("message", "")
    event_dict.setdefault("request_id", get_request_id())
    event_dict.setdefault("correlation_id", get_correlation_id())
    event_dict.setdefault("job_id", get_job_id())
    event_dict.setdefault("entity_type", None)
    event_dict.setdefault("entity_id", None)
    event_dict.setdefault("exception_type", None)
    event_dict.setdefault("exception_message", None)
    exc_info = event_dict.get("exc_info")
    if isinstance(exc_info, BaseException):
        event_dict["exception_type"] = type(exc_info).__name__
        event_dict["exception_message"] = str(exc_info)
    return event_dict
