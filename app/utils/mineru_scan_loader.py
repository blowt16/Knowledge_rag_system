"""MinerU 扫描件 PDF 解析 — 基于 mineru SDK 直连（替代 langchain-mineru）。

专用于 scan_pdf 分支，直接调用 mineru.MinerU API：
  - 整份 PDF 一次提交 → 解析 content_list.json 按 page_idx 分组
  - 保存 result.images（图表/内嵌图片）到本地，按 content_list 归因到页码
  - 逐页重建 markdown → metadata.image_paths 支持检索溯源
"""
import json
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
MINERU_MAX_PAGES = int(os.getenv("MINERU_MAX_PAGES_PER_BATCH",
    str(get_config("mineru_max_pages_per_batch", 200))))


# ============================================================
# content_list 块 → Markdown 转换
# ============================================================

def _blocks_to_markdown(
    blocks: list[dict],
    image_map: dict[str, str],
    user_id: str,
    md5_hex: str,
) -> str:
    """将同一页的 content_list 块列表转换为 Markdown 文本。

    image_map: {MinerU原始路径 (images/img_0.png) → 本地文件名 (p3_i0.png)}
    """
    lines: list[str] = []
    for block in blocks:
        block_type = block.get("type", "text")

        if block_type in ("text", "header", "footer", "paragraph"):
            text = block.get("text", "")
            text_level = block.get("text_level", 0)
            if text_level and text_level > 0:
                prefix = "#" * min(text_level, 6)
                lines.append(f"{prefix} {text}")
            elif text.strip():
                lines.append(text)

        elif block_type == "image":
            img_path = block.get("img_path", "")
            local_name = image_map.get(img_path)
            if local_name:
                lines.append(
                    f"![图表](/images/{user_id}/{md5_hex}/mineru/{local_name})"
                )
            else:
                alt = block.get("text", "") or "图表"
                lines.append(f"[{alt}]")

        elif block_type == "table":
            table_body = block.get("table_body", "")
            img_path = block.get("img_path", "")
            if table_body:
                lines.append(table_body)
            elif img_path:
                local_name = image_map.get(img_path)
                if local_name:
                    lines.append(
                        f"![表格](/images/{user_id}/{md5_hex}/mineru/{local_name})"
                    )
                else:
                    lines.append("[表格]")

        elif block_type == "equation":
            text = block.get("text", "")
            fmt = block.get("text_format", "block")
            if fmt == "inline":
                lines.append(f"${text}$")
            else:
                lines.append(f"$$\n{text}\n$$")

        elif block_type == "code":
            code = block.get("code_body", block.get("text", ""))
            if code.strip():
                lines.append(f"```\n{code}\n```")

        elif block_type == "list":
            text = block.get("text", "")
            for item in text.split("\n"):
                stripped = item.strip()
                if stripped:
                    lines.append(f"- {stripped}")

        else:
            # 未知类型，fallback 输出文本
            text = block.get("text", "")
            if text.strip():
                lines.append(text)

    return "\n\n".join(lines)


# ============================================================
# 单批结果 → Document 列表
# ============================================================

def _build_documents(
    content_list: list[dict],
    images: list,
    mineru_img_dir: Path,
    file_path: str,
    user_id: str,
    md5_hex: str,
    page_filter: set | None,
) -> list[Document]:
    """将一批 MinerU 结果转换为 Document 列表。"""
    from app.utils.path_tool import get_data_path

    # 按 page_idx 分组块
    pages_blocks: dict[int, list[dict]] = {}
    for block in content_list:
        page_num = block.get("page_idx", 0) + 1
        if page_filter is not None and page_num not in page_filter:
            continue
        pages_blocks.setdefault(page_num, []).append(block)

    # 图片→页码映射
    img_path_to_page: dict[str, int] = {}
    for block in content_list:
        if block.get("type") in ("image", "table"):
            p = block.get("img_path", "")
            if p:
                img_path_to_page[p] = block.get("page_idx", 0) + 1

    # 保存图片
    image_map: dict[str, str] = {}
    page_images: dict[int, list[str]] = {}
    image_counts: dict[int, int] = {}

    for idx, img in enumerate(images):
        try:
            ext = img.name.rsplit(".", 1)[-1] if "." in img.name else "png"
            page_num = img_path_to_page.get(img.path, 1)
            local_name = f"p{page_num}_i{idx}.{ext}"
            img_full_path = mineru_img_dir / local_name
            img_full_path.write_bytes(img.data)
            relative = img_full_path.relative_to(get_data_path()).as_posix()
            image_map[img.path] = local_name
            page_images.setdefault(page_num, []).append(relative)
            image_counts[page_num] = image_counts.get(page_num, 0) + 1
        except OSError as e:
            logger.warning(f"【scan_pdf】图片{idx}({img.name})保存失败: {e}")

    # 逐页组装 Document
    documents: list[Document] = []
    for page_num in sorted(pages_blocks.keys()):
        blocks = pages_blocks[page_num]
        markdown = _blocks_to_markdown(blocks, image_map, user_id, md5_hex)
        if not markdown.strip():
            logger.warning(f"【scan_pdf】第{page_num}页 content_list 无文本")
            continue

        mineru_paths = page_images.get(page_num, [])
        meta = {
            "source": file_path, "page": page_num,
            "has_images": len(mineru_paths) > 0,
            "ocr_engine": f"mineru_{MINERU_MODE}",
            "scan_branch": "mineru", "toc": "[]", "chapter_count": 0,
        }
        if mineru_paths:
            meta["image_paths"] = mineru_paths

        documents.append(Document(page_content=markdown.strip(), metadata=meta))
        imgs = image_counts.get(page_num, 0)
        logger.info(
            f"【scan_pdf】第{page_num}页 MinerU 成功"
            + (f", 图片={imgs}" if imgs else "")
        )

    return documents


# ============================================================
# 主入口
# ============================================================

async def process_scan_pdf_mineru(
    pdf_path: str,
    file_path: str,
    page_image_map: dict[int, list[str]],
    user_id: str = "",
    md5_hex: str = "",
    progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
    page_filter: set | None = None,
    on_batch: Callable[[list[Document], int, int], Awaitable[None]] | None = None,
) -> tuple[list[Document], dict]:
    """MinerU 扫描件 PDF 解析入口，兼容 _process_scan_pdf 签名。

    流程: 分批提交 → content_list 按 page_idx 分组 → 保存图片 → 组装 Document。
    若提供 on_batch 回调，每批完成后立即触发（不累积），实现流水线处理。
    """
    from mineru import MinerU
    from pypdf import PdfReader, PdfWriter

    from app.utils.path_tool import get_data_path, get_image_dir

    pdf_name = Path(pdf_path).name

    # ── 准备图片输出目录 ──
    mineru_img_dir = get_image_dir(f"{user_id}/{md5_hex}/mineru")
    mineru_img_dir.mkdir(parents=True, exist_ok=True)

    # ── 创建 MinerU 客户端 ──
    _token = MINERU_TOKEN if MINERU_MODE == "precision" else None
    client = MinerU(token=_token)

    # ── 分批提交 PDF（超过 MINERU_MAX_PAGES 页时自动拆分） ──
    reader = PdfReader(str(pdf_path))
    total_pages = len(reader.pages)
    batch_size = MINERU_MAX_PAGES

    # 按 batch_size 分组页码: [(1, 200), (201, 400), ...]
    batches: list[tuple[int, int]] = []
    for start in range(1, total_pages + 1, batch_size):
        end = min(start + batch_size - 1, total_pages)
        batches.append((start, end))

    all_markdown: str = ""  # flash 模式 fallback
    processed: list[Document] = []  # 无 on_batch 回调时累积

    from tempfile import TemporaryDirectory

    for batch_idx, (batch_start, batch_end) in enumerate(batches):
        batch_pages = batch_end - batch_start + 1
        if progress_callback:
            if len(batches) > 1:
                await progress_callback(
                    "loading",
                    f"MinerU 解析中 ({batch_start}-{batch_end}/{total_pages} 页)...",
                )
            else:
                await progress_callback("loading", f"MinerU 解析中 ({pdf_name})...")

        # 拆分该批次的 PDF
        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            batch_pdf_path = tmpdir_path / f"batch_{batch_start}_{batch_end}.pdf"
            writer = PdfWriter()
            for pn in range(batch_start - 1, batch_end):
                writer.add_page(reader.pages[pn])
            with open(batch_pdf_path, "wb") as f:
                writer.write(f)

            _batch_size_mb = batch_pdf_path.stat().st_size / (1024 * 1024)
            logger.info(
                f"【scan_pdf】提交批次 {batch_idx + 1}/{len(batches)}: "
                f"第{batch_start}-{batch_end}页 ({batch_pages}页, {_batch_size_mb:.1f}MB), "
                f"预计等待 {MINERU_TIMEOUT // 60} 分钟内返回..."
            )

            # 调用 MinerU API
            try:
                if MINERU_MODE == "flash":
                    result = client.flash_extract(
                        str(batch_pdf_path),
                        language=MINERU_LANGUAGE,
                        timeout=MINERU_TIMEOUT,
                    )
                else:
                    result = client.extract(
                        str(batch_pdf_path),
                        language=MINERU_LANGUAGE,
                        timeout=MINERU_TIMEOUT,
                        formula=True,
                        table=True,
                    )
            except Exception as e:
                logger.error(f"【scan_pdf】MinerU API 异常 (批次 {batch_start}-{batch_end}): {e}")
                raise ValueError(
                    f"【scan_pdf】MinerU 解析失败: {e}. "
                    f"请检查文件后重新上传: {pdf_name}"
                ) from e

        if result.state != "done":
            raise ValueError(
                f"【scan_pdf】MinerU 解析失败 (批次 {batch_start}-{batch_end}): "
                f"state={result.state}, error={result.error}. 文件: {pdf_name}"
            )

        logger.info(
            f"【scan_pdf】批次 {batch_idx + 1}/{len(batches)} 完成: "
            f"第{batch_start}-{batch_end}页, "
            f"content_list={len(result.content_list or [])} 块, "
            f"images={len(result.images or [])} 张"
        )

        # 偏移 content_list 的 page_idx + 立即处理本批图片和文档
        page_offset = batch_start - 1
        batch_content_list = []
        for block in (result.content_list or []):
            block["page_idx"] = block.get("page_idx", 0) + page_offset
            batch_content_list.append(block)

        # flash 模式：拼接 markdown
        if all_markdown or result.markdown:
            if all_markdown:
                all_markdown += f"\n\n---\n\n"
            all_markdown += (result.markdown or "")

        # 构建本批 Documents
        batch_docs = _build_documents(
            batch_content_list, result.images or [],
            mineru_img_dir, file_path, user_id, md5_hex, page_filter,
        )

        if on_batch:
            await on_batch(batch_docs, batch_start, batch_end)
        else:
            processed.extend(batch_docs)

    # ── 无 on_batch 回调时的 fallback：flash 模式 markdown 分页 ──
    if not on_batch and not processed and all_markdown:
        logger.info("【scan_pdf】无 content_list，使用 markdown 分页")
        pages = _split_markdown_by_page(all_markdown)
        for page_num, page_md in enumerate(pages, start=1):
            if page_filter is not None and page_num not in page_filter:
                continue
            if not page_md.strip():
                continue
            processed.append(Document(
                page_content=page_md.strip(),
                metadata={
                    "source": file_path, "page": page_num,
                    "has_images": False,
                    "ocr_engine": f"mineru_{MINERU_MODE}",
                    "scan_branch": "mineru", "toc": "[]", "chapter_count": 0,
                },
            ))

    if on_batch:
        # 流水线模式：文档已通过回调逐批送出，无需检查
        logger.info(f"【scan_pdf】MinerU 完成（流水线模式）")
    else:
        if not processed:
            raise ValueError(f"【scan_pdf】MinerU 解析结果为空: {pdf_name}")

        # OCR 文本提取失败检查（与旧管线一致）
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

        total_images = sum(
            len(d.metadata.get("image_paths", [])) for d in processed
        )
        logger.info(
            f"【scan_pdf】MinerU 完成: {len(processed)} 页, "
            f"图片={total_images} "
            f"(mode={MINERU_MODE}, language={MINERU_LANGUAGE})"
        )

    return processed, {}


# ============================================================
# Fallback: markdown 分页（无 content_list 时）
# ============================================================

def _split_markdown_by_page(markdown: str) -> list[str]:
    """按常见分页标记拆分 markdown。"""
    import re

    # 多种分页标记
    patterns = [
        r'\n---\s*\n',           # Markdown 水平线
        r'\n\*\*\*\s*\n',        # ***
        r'\n{3,}',               # 3+ 连续空行（弱信号，最后尝试）
    ]

    for pattern in patterns:
        parts = re.split(pattern, markdown)
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) > 1:
            return parts

    return [markdown]


def _replace_images_in_text(
    text: str,
    image_map: dict[str, str],
    user_id: str,
    md5_hex: str,
) -> str:
    """替换文本中的 MinerU 图片引用为服务器路径。"""
    for original_path, local_name in image_map.items():
        if original_path in text:
            server_path = f"/images/{user_id}/{md5_hex}/mineru/{local_name}"
            text = text.replace(
                f"]({original_path})",
                f"]({server_path})",
            )
    return text
