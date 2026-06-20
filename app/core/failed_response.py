"""统一异常处理 — 全局异常处理器 + 异常响应格式。"""
from fastapi import Request
from fastapi.responses import JSONResponse


class AppException(Exception):
    """应用级异常基类。"""

    def __init__(self, message: str, code: int = 400, detail: str = ""):
        self.message = message
        self.code = code
        self.detail = detail
        super().__init__(message)


class DocumentLoadException(AppException):
    """文档加载失败异常，携带诊断信息。"""

    def __init__(self, diagnosis: dict):
        super().__init__(
            message=diagnosis.get("detail", "文档加载失败"),
            code=400,
            detail=str(diagnosis),
        )
        self.diagnosis = diagnosis


async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.code if exc.code >= 400 else 400,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": exc.detail,
        },
    )


async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "code": 500,
            "message": "服务器内部错误",
            "detail": str(exc),
        },
    )
