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
) -> tuple[list, dict]:
    """PDF 统一解析入口（异步）：加密检测 → 多模态三分支解析。

    委托给 pdf_multimodal_loader.load_pdf_async()：
      - text_pdf:   pdfplumber 直接提取（秒级）
      - text_mix_pdf: pdfplumber + PyMuPDF 裁切 + 多模态 VL
      - scan_pdf:   MinerU (langchain-mineru) 云端解析

    返回: (documents, degradation) — degradation 为空 dict 表示完美解析，非空表示部分内容降级
    """
    from app.utils.pdf_multimodal_loader import load_pdf_async
    return await load_pdf_async(
        str(file_path),
        user_id=user_id,
        md5_hex=md5_hex,
        original_filename=original_filename,
        progress_callback=progress_callback,
    )
