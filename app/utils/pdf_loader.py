"""PDF 解析入口 — 委托给多模态三分支解析器。"""
from pathlib import Path
from typing import Callable, Awaitable

from app.utils.log_tool import get_logger

logger = get_logger(__name__)


async def load_pdf(
    file_path: str | Path,
    user_id: str = "",
    md5_hex: str = "",
    original_filename: str = "",
    progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
    on_batch: Callable[[list, int, int], Awaitable[None]] | None = None,
) -> tuple[list, dict]:
    """PDF 统一解析入口（异步）：加密检测 → 多模态三分支解析。"""
    from app.utils.pdf_multimodal_loader import load_pdf_async
    return await load_pdf_async(
        str(file_path),
        user_id=user_id,
        md5_hex=md5_hex,
        original_filename=original_filename,
        progress_callback=progress_callback,
        on_batch=on_batch,
    )
