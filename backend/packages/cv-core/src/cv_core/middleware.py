from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from cv_contracts import ApiError
from cv_utils import get_correlation_id

RequestIntResolver = Callable[[Request], int]
RequestFloatResolver = Callable[[Request], float]
NextHandler = Callable[[Request], Awaitable[Response]]


def _error_response(*, code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ApiError(
            code=code,
            message=message,
            status_code=status_code,
            correlation_id=get_correlation_id(),
        ).model_dump(),
    )


class ContentLengthLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, limit_resolver: RequestIntResolver) -> None:
        super().__init__(app)
        self._limit_resolver = limit_resolver

    async def dispatch(self, request: Request, call_next: NextHandler) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is None:
            return await call_next(request)
        try:
            declared_length = int(content_length)
        except ValueError:
            return _error_response(
                code="request_body_invalid",
                message="The request Content-Length header is invalid.",
                status_code=400,
            )
        limit = self._limit_resolver(request)
        if declared_length > limit:
            return _error_response(
                code="request_body_too_large",
                message="The request body exceeds the configured size limit.",
                status_code=413,
            )
        return await call_next(request)


class RequestTimeoutMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, timeout_resolver: RequestFloatResolver) -> None:
        super().__init__(app)
        self._timeout_resolver = timeout_resolver

    async def dispatch(self, request: Request, call_next: NextHandler) -> Response:
        try:
            return await asyncio.wait_for(
                call_next(request),
                timeout=max(self._timeout_resolver(request), 0.1),
            )
        except TimeoutError:
            return _error_response(
                code="request_timeout",
                message="The request exceeded the service timeout budget.",
                status_code=504,
            )
