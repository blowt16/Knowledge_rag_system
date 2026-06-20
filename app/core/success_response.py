"""统一成功响应格式。"""
from typing import Any


def success_response(data: Any = None, message: str = "success", code: int = 200) -> dict:
    return {"code": code, "message": message, "data": data}
