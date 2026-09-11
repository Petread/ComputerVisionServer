from __future__ import annotations

from typing import Any


class PlatformError(Exception):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int = 400,
        detail: str | None = None,
        retryable: bool = False,
        validation_errors: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail
        self.retryable = retryable
        self.validation_errors = validation_errors or []
        self.metadata = metadata or {}


class AuthenticationError(PlatformError):
    def __init__(self, message: str = "Authentication failed") -> None:
        super().__init__(
            code="authentication_failed",
            message=message,
            status_code=401,
            retryable=False,
        )


class AuthorizationError(PlatformError):
    def __init__(self, message: str = "Authorization failed") -> None:
        super().__init__(
            code="authorization_failed",
            message=message,
            status_code=403,
            retryable=False,
        )


class NotFoundError(PlatformError):
    def __init__(self, message: str = "Resource not found", *, detail: str | None = None) -> None:
        super().__init__(
            code="resource_not_found",
            message=message,
            status_code=404,
            detail=detail,
            retryable=False,
        )
