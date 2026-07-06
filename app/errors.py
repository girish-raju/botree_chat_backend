"""Application error types and FastAPI exception handler registration."""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)


class AppError(Exception):
    """Base application error. Subclass for specific error conditions."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str = "Internal server error") -> None:
        self.message = message
        super().__init__(message)


class AuthError(AppError):
    status_code = 401
    code = "auth_error"

    def __init__(self, message: str = "Authentication failed") -> None:
        super().__init__(message)


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"

    def __init__(self, message: str = "Forbidden") -> None:
        super().__init__(message)


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"

    def __init__(self, message: str = "Not found") -> None:
        super().__init__(message)


class SQLSafetyError(AppError):
    status_code = 400
    code = "sql_blocked"

    def __init__(self, message: str = "SQL statement blocked by safety checks") -> None:
        super().__init__(message)


class RBACError(AppError):
    status_code = 400
    code = "rbac_blocked"

    def __init__(self, message: str = "Blocked by RBAC policy") -> None:
        super().__init__(message)


class UpstreamLLMError(AppError):
    status_code = 502
    code = "llm_error"

    def __init__(self, message: str = "Upstream LLM provider error") -> None:
        super().__init__(message)


def _request_id(request: Request) -> str:
    state = getattr(request, "state", None)
    return getattr(state, "request_id", "") or request.headers.get("x-request-id", "")


def _error_response(request: Request, status_code: int, code: str, message: str) -> JSONResponse:
    request_id = _request_id(request)
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
            }
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register handlers that translate errors into a consistent JSON envelope."""

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "app_error",
            code=exc.code,
            status_code=exc.status_code,
            message=exc.message,
            path=request.url.path,
        )
        return _error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(Exception)
    async def _handle_unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled_exception",
            path=request.url.path,
            exc_info=exc,
        )
        return _error_response(
            request,
            500,
            "internal_error",
            "An unexpected error occurred.",
        )


__all__ = [
    "AppError",
    "AuthError",
    "ForbiddenError",
    "NotFoundError",
    "SQLSafetyError",
    "RBACError",
    "UpstreamLLMError",
    "register_exception_handlers",
]
