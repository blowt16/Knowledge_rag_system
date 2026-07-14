"""MinerU 扫描件 PDF 解析 — 基于 langchain-mineru (LangChain 官方集成)。

专用于 scanf_pdf 分支，替代 PaddleOCR / Kitty-Doc 管线。
委托给 MinerULoader 完成 PDF → Document 转换，本地仅补充元数据。
"""
import os
from pathlib import Path
from typing import Callable, Awaitable

from langchain_core.documents import Document

from app.config.loader import get_config
from app.utils.log_tool import get_logger

logger = get_logger(__name__)

# ============================================================
# 模块级配置 (chroma.yaml)
# ============================================================
MINERU_MODE = get_config("mineru_mode", "precision")
MINERU_LANGUAGE = get_config("mineru_language", "ch")
MINERU_TOKEN = get_config("mineru_token", "") or os.getenv("MINERU_TOKEN", "")
MINERU_TIMEOUT = int(get_config("mineru_timeout", 1200))


async def process_scan_pdf_mineru(
    pdf_path: str,
    file_path: str,
    page_image_map: dict[int, list[str]],
    user_id: str = "",
    md5_hex: str = "",
    progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
    page_filter: set | None = None,
) -> tuple[list[Document], dict]:
    """MinerU 扫描件 PDF 解析入口，兼容 _process_scan_pdf 签名。

    使用 langchain-mineru 的 MinerULoader 完成 PDF 解析。
    """
    from langchain_mineru import MinerULoader

    pdf_name = Path(pdf_path).name

    if progress_callback:
        await progress_callback("loading", f"MinerU 解析中 ({pdf_name})...")

    # 构建 MinerU 参数
    loader_kwargs: dict = {
        "source": pdf_path,
        "mode": MINERU_MODE,
        "language": MINERU_LANGUAGE,
        "split_pages": True,
        "timeout": MINERU_TIMEOUT,
    }
    if MINERU_TOKEN:
        loader_kwargs["token"] = MINERU_TOKEN

    try:
        loader = MinerULoader(**loader_kwargs)
        docs = loader.load()
    except Exception as e:
        logger.warning(f"【scan_pdf】MinerU 解析异常: {e}")
        raise ValueError(
            f"【scan_pdf】MinerU 解析失败: {e}. "
            f"请检查文件后重新上传: {pdf_name}"
        ) from e

    if not docs:
        raise ValueError(
            f"【scan_pdf】MinerU 解析结果为空: {pdf_name}"
        )

    # 补充元数据 + 过滤页码 + 逐页日志
    processed = []
    for doc in docs:
        page_num = doc.metadata.get("page", 0)
        if page_filter and page_num not in page_filter:
            continue
        images_on_page = page_image_map.get(page_num, [])
        doc.metadata.update({
            "source": file_path,
            "has_images": len(images_on_page) > 0,
            "ocr_engine": f"mineru_{MINERU_MODE}",
            "scan_branch": "mineru",
            "toc": "[]",
            "chapter_count": 0,
        })
        if images_on_page:
            doc.metadata["image_paths"] = images_on_page
        # 逐页日志 (与旧 PaddleOCR 格式一致)
        if doc.page_content.strip():
            logger.info(f"【scan_pdf】第{page_num}页 MinerU 成功")
        else:
            logger.warning(f"【scan_pdf】第{page_num}页 MinerU 无结果")
        processed.append(doc)

    if not processed:
        raise ValueError(
            f"【scan_pdf】MinerU 解析结果为空 (过滤后): {pdf_name}"
        )

    # OCR 文本提取失败检查 (与旧管线一致)
    failed_pages = [
        d.metadata.get("page", "?") for d in processed
        if not d.page_content.strip()
    ]
    if failed_pages:
        raise ValueError(
            f"【scan_pdf】{len(failed_pages)} 页文本提取完全失败: "
            f"页码 {failed_pages[:10]}{'...' if len(failed_pages) > 10 else ''}. "
            f"扫描件 OCR 解析不可靠，请检查文件后重新上传: {pdf_name}"
        )

    logger.info(
        f"【scan_pdf】MinerU 完成: {len(processed)} 页 "
        f"(mode={MINERU_MODE}, language={MINERU_LANGUAGE})"
    )
    return processed, {}
