from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from cv_contracts import ApiError, ValidationErrorDetail
from cv_utils import PlatformError, get_correlation_id


def _build_payload(
    *,
    code: str,
    message: str,
    status_code: int,
    detail: str | None = None,
    retryable: bool = False,
    validation_errors: list[ValidationErrorDetail] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return ApiError(
        code=code,
        message=message,
        detail=detail,
        status_code=status_code,
        correlation_id=get_correlation_id(),
        retryable=retryable,
        validation_errors=validation_errors or [],
        metadata=metadata or {},
    ).model_dump()


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(PlatformError)
    async def handle_platform_error(_request: Request, exc: PlatformError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_build_payload(
                code=exc.code,
                message=exc.message,
                detail=exc.detail,
                status_code=exc.status_code,
                retryable=exc.retryable,
                validation_errors=[
                    ValidationErrorDetail.model_validate(item) for item in exc.validation_errors
                ],
                metadata=exc.metadata,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        errors = exc.errors()
        detail = "; ".join(str(item.get("msg")) for item in errors[:3]) or "Invalid request payload."
        validation_errors = [
            ValidationErrorDetail(
                field=".".join(str(part) for part in item.get("loc", [])[1:]) or None,
                location=list(item.get("loc", [])),
                message=str(item.get("msg") or "Invalid value."),
                error_type=str(item.get("type")) if item.get("type") else None,
            )
            for item in errors
        ]
        return JSONResponse(
            status_code=422,
            content=_build_payload(
                code="invalid_request",
                message="Request validation failed.",
                detail=detail,
                status_code=422,
                retryable=False,
                validation_errors=validation_errors,
                metadata={"errors": errors},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else None
        message = detail or f"HTTP {exc.status_code}"
        return JSONResponse(
            status_code=exc.status_code,
            content=_build_payload(
                code=f"http_{exc.status_code}",
                message=message,
                detail=detail,
                status_code=exc.status_code,
                retryable=exc.status_code >= 500,
            ),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content=_build_payload(
                code="internal_error",
                message="An unexpected error occurred",
                detail=str(exc),
                status_code=500,
                retryable=True,
            ),
        )
