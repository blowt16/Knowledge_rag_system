"""统一异常与全局处理。

口径（§3.5.4）：校验失败 → **稳定错误码，不返回模型原文、不泄露提示词**。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    """业务异常基类。code 是稳定标识，message 是给人看的话。"""

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str = "", *, code: str | None = None,
                 status_code: int | None = None):
        super().__init__(message)
        self.message = message or self.code
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    """409 —— 会话正在处理另一个请求（§3.2.4）。"""
    status_code = 409
    code = "session_busy"


class TooManyRequests(AppError):
    status_code = 429
    code = "too_many_requests"


class UpstreamError(AppError):
    status_code = 502
    code = "upstream_error"


def install_handlers(app: FastAPI) -> None:
    from app.core.telemetry import current_trace_id

    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "message": exc.message,
                # 用户报障时可直接定位（§3.2.3.1）
                "trace_id": current_trace_id(),
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": f"http_{exc.status_code}",
                "message": str(exc.detail),
                "trace_id": current_trace_id(),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": "请求参数不合法",
                "detail": exc.errors(),
                "trace_id": current_trace_id(),
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # 记完整堆栈到日志，但**不把内部细节返回给客户端**
        logger.exception("未处理异常", extra={"event": "error.unhandled",
                                            "node": str(request.url.path)})
        return JSONResponse(
            status_code=500,
            content={
                "code": "internal",
                "message": "服务器内部错误",
                "trace_id": current_trace_id(),
            },
        )
