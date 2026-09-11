from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from cv_utils import clear_request_context, set_request_context


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Response],
    ) -> Response:
        request_id = request.headers.get("x-request-id") or f"req_{uuid4().hex}"
        correlation_id = request.headers.get("x-correlation-id") or request_id
        set_request_context(request_id=request_id, correlation_id=correlation_id)
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            correlation_id=correlation_id,
            job_id=None,
        )
        try:
            response = await call_next(request)
        finally:
            clear_request_context()
            structlog.contextvars.unbind_contextvars("request_id", "correlation_id", "job_id")
        response.headers["x-request-id"] = request_id
        response.headers["x-correlation-id"] = correlation_id
        return response
