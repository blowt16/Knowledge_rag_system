"""MinerU 扫描件 PDF 解析 — 基于 mineru SDK 直连（替代 langchain-mineru）。

专用于 scan_pdf 分支，直接调用 mineru.MinerU API：
  - 整份 PDF 一次提交 → 解析 content_list.json 按 page_idx 分组
  - 保存 result.images（图表/内嵌图片）到本地，按 content_list 归因到页码
  - 逐页重建 markdown → metadata.image_paths + metadata.mineru_images 支持检索溯源
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
) -> tuple[list[Document], dict]:
    """MinerU 扫描件 PDF 解析入口，兼容 _process_scan_pdf 签名。

    流程: 整份 PDF 一次提交 → content_list 按 page_idx 分组 → 保存图片 → 组装 Document。
    """
    from mineru import MinerU

    from app.utils.path_tool import get_data_path, get_image_dir

    pdf_name = Path(pdf_path).name

    if progress_callback:
        await progress_callback("loading", f"MinerU 解析中 ({pdf_name})...")

    # ── 准备图片输出目录 ──
    mineru_img_dir = get_image_dir(f"{user_id}/{md5_hex}/mineru")
    mineru_img_dir.mkdir(parents=True, exist_ok=True)

    # ── 创建 MinerU 客户端 ──
    _token = MINERU_TOKEN if MINERU_MODE == "precision" else None
    client = MinerU(token=_token)

    # ── 提交整份 PDF（一次 API 调用） ──
    if progress_callback:
        await progress_callback("loading", f"MinerU 解析中 ({pdf_name})...")

    try:
        if MINERU_MODE == "flash":
            result = client.flash_extract(
                pdf_path,
                language=MINERU_LANGUAGE,
                timeout=MINERU_TIMEOUT,
            )
        else:
            result = client.extract(
                pdf_path,
                language=MINERU_LANGUAGE,
                timeout=MINERU_TIMEOUT,
                formula=True,
                table=True,
            )
    except Exception as e:
        logger.error(f"【scan_pdf】MinerU API 异常: {e}")
        raise ValueError(
            f"【scan_pdf】MinerU 解析失败: {e}. "
            f"请检查文件后重新上传: {pdf_name}"
        ) from e

    if result.state != "done":
        raise ValueError(
            f"【scan_pdf】MinerU 解析失败: state={result.state}, "
            f"error={result.error}. 文件: {pdf_name}"
        )

    # ── 解析 content_list，按 page_idx 分组 ──
    content_list = result.content_list or []
    images = result.images or []
    markdown_full = result.markdown or ""

    # 按 page_idx 分组块
    pages_blocks: dict[int, list[dict]] = {}
    for block in content_list:
        page_idx = block.get("page_idx", 0)  # 0-indexed
        page_num = page_idx + 1  # 1-indexed
        if page_filter is not None and page_num not in page_filter:
            continue
        pages_blocks.setdefault(page_num, []).append(block)

    # ── 处理图片：建立 img_path → 本地文件映射 ──
    # 如果 content_list 有 page_idx，用 content_list 归因图片到页码
    # 否则所有图片归到最后一页（fallback）
    image_map: dict[str, str] = {}          # MinerU原始路径 → 本地文件名
    page_images: dict[int, list[str]] = {}  # 页码 → 图片相对路径列表
    page_mineru_images: dict[int, list[dict]] = {}  # 页码 → mineru_images 详情

    # 先从 content_list 中提取图片→页码映射
    img_path_to_page: dict[str, int] = {}
    for block in content_list:
        block_type = block.get("type", "")
        if block_type in ("image", "table"):
            img_path = block.get("img_path", "")
            page_idx = block.get("page_idx", 0)
            if img_path:
                img_path_to_page[img_path] = page_idx + 1

    # 保存所有 MinerU 图片
    for idx, img in enumerate(images):
        try:
            ext = img.name.rsplit(".", 1)[-1] if "." in img.name else "png"
            # 用 content_list 中的 page_idx 确定页码，fallback 用文件名推测
            page_num = img_path_to_page.get(img.path, 1)
            local_name = f"p{page_num}_i{idx}.{ext}"
            img_full_path = mineru_img_dir / local_name
            img_full_path.write_bytes(img.data)

            relative = img_full_path.relative_to(get_data_path()).as_posix()

            # 记录映射
            image_map[img.path] = local_name
            page_images.setdefault(page_num, []).append(relative)
            page_mineru_images.setdefault(page_num, []).append({
                "name": img.name,
                "path": relative,
                "idx": idx,
                "original_ref": img.path,
            })
        except OSError as e:
            logger.warning(f"【scan_pdf】图片{idx}({img.name})保存失败: {e}")
            continue

    # ── 逐页组装 Document ──
    processed: list[Document] = []

    if pages_blocks:
        # 有 content_list → 逐页重建 markdown
        for page_num in sorted(pages_blocks.keys()):
            blocks = pages_blocks[page_num]
            markdown = _blocks_to_markdown(blocks, image_map, user_id, md5_hex)

            if not markdown.strip():
                logger.warning(f"【scan_pdf】第{page_num}页 content_list 无文本")
                continue

            # 合并图片路径
            embedded = page_image_map.get(page_num, [])
            mineru_paths = page_images.get(page_num, [])
            all_image_paths = embedded + mineru_paths

            meta = {
                "source": file_path,
                "page": page_num,
                "has_images": len(all_image_paths) > 0,
                "ocr_engine": f"mineru_{MINERU_MODE}",
                "scan_branch": "mineru",
                "toc": "[]",
                "chapter_count": 0,
            }
            if all_image_paths:
                meta["image_paths"] = all_image_paths
            if page_mineru_images.get(page_num):
                meta["mineru_images"] = page_mineru_images[page_num]

            processed.append(Document(
                page_content=markdown.strip(),
                metadata=meta,
            ))

            imgs = len(page_mineru_images.get(page_num, []))
            logger.info(
                f"【scan_pdf】第{page_num}页 MinerU 成功"
                + (f", 图片={imgs}" if imgs else "")
            )
    else:
        # 无 content_list（flash 模式）→ 尝试用 markdown 分页
        logger.info("【scan_pdf】无 content_list，使用 markdown 分页")
        pages = _split_markdown_by_page(markdown_full)

        for page_num, page_md in enumerate(pages, start=1):
            if page_filter is not None and page_num not in page_filter:
                continue
            if not page_md.strip():
                continue

            embedded = page_image_map.get(page_num, [])
            mineru_paths = page_images.get(page_num, [])
            all_image_paths = embedded + mineru_paths

            # 替换图片引用
            page_md = _replace_images_in_text(page_md, image_map, user_id, md5_hex)

            meta = {
                "source": file_path,
                "page": page_num,
                "has_images": len(all_image_paths) > 0,
                "ocr_engine": f"mineru_{MINERU_MODE}",
                "scan_branch": "mineru",
                "toc": "[]",
                "chapter_count": 0,
            }
            if all_image_paths:
                meta["image_paths"] = all_image_paths
            if page_mineru_images.get(page_num):
                meta["mineru_images"] = page_mineru_images[page_num]

            processed.append(Document(
                page_content=page_md.strip(),
                metadata=meta,
            ))

            logger.info(
                f"【scan_pdf】第{page_num}页 MinerU 成功（markdown分页）"
            )

    if not processed:
        raise ValueError(
            f"【scan_pdf】MinerU 解析结果为空: {pdf_name}"
        )

    # ── OCR 文本提取失败检查（与旧管线一致） ──
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
