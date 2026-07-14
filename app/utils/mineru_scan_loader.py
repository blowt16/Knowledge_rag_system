"""MinerU 扫描件 PDF 解析 — 基于 mineru SDK 直连（替代 langchain-mineru）。

专用于 scan_pdf 分支，直接调用 mineru.MinerU API：
  - pypdf 逐页拆分 → 并发提交 → 获取 ExtractResult
  - 保存 result.images（图表/内嵌图片）到本地
  - markdown 图片引用替换为服务器路径
  - metadata.image_paths + metadata.mineru_images 支持检索溯源
"""
import asyncio
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
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
MINERU_CONCURRENCY = int(os.getenv("MINERU_CONCURRENCY",
    str(get_config("mineru_concurrency", 3))))


# ============================================================
# Markdown 图片引用替换
# ============================================================

# MinerU 返回的 markdown 中图片引用格式: ![](images/img_0.png) 或 ![alt](images/img_0.png)
_IMG_REF_PATTERN = re.compile(r'!\[([^\]]*)\]\((images/[^)]+)\)')


def _replace_markdown_image_refs(
    markdown: str,
    image_map: dict[str, str],
    user_id: str,
    md5_hex: str,
) -> str:
    """将 MinerU markdown 中的图片引用替换为服务器相对路径。

    image_map: {MinerU原始路径 (images/img_0.png) → 本地文件名 (p3_i0.png)}
    """
    def _replacer(match: re.Match) -> str:
        alt = match.group(1) or "图表"
        original_path = match.group(2)
        local_name = image_map.get(original_path)
        if local_name:
            # 服务器相对路径，前端可直接渲染
            server_path = f"/images/{user_id}/{md5_hex}/mineru/{local_name}"
            return f"![{alt}]({server_path})"
        # 未匹配到映射，保留原文（理论上不应该发生）
        return match.group(0)

    return _IMG_REF_PATTERN.sub(_replacer, markdown)


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

    流程: pypdf 分页 → mineru SDK 并发提交 → 保存图片 → 替换markdown引用 → 组装 Document。
    """
    from mineru import MinerU
    from pypdf import PdfReader, PdfWriter

    from app.utils.path_tool import get_data_path, get_image_dir

    pdf_name = Path(pdf_path).name

    if progress_callback:
        await progress_callback("loading", f"MinerU 解析中 ({pdf_name})...")

    # ── 准备图片输出目录 ──
    mineru_img_dir = get_image_dir(f"{user_id}/{md5_hex}/mineru")
    mineru_img_dir.mkdir(parents=True, exist_ok=True)

    # ── 确定待处理页码 ──
    reader = PdfReader(str(pdf_path))
    total_pages = len(reader.pages)
    pages_to_process = [
        pn for pn in range(1, total_pages + 1)
        if page_filter is None or pn in page_filter
    ]

    if not pages_to_process:
        raise ValueError(
            f"【scan_pdf】没有需要处理的页面: {pdf_name}"
        )

    if progress_callback:
        await progress_callback(
            "loading",
            f"MinerU 解析中 ({pdf_name}, {len(pages_to_process)}/{total_pages} 页)...",
        )

    # ── 创建 MinerU 客户端 (flash 模式不需要 token) ──
    _token = MINERU_TOKEN if MINERU_MODE == "precision" else None
    client = MinerU(token=_token)

    # ── 并发控制 ──
    sem = asyncio.Semaphore(MINERU_CONCURRENCY)
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=MINERU_CONCURRENCY)

    # 收集结果
    results_by_page: dict[int, dict] = {}
    errors: list[tuple[int, str]] = []

    async def _process_one_page(page_num: int) -> None:
        """处理单个页面：拆分 → MinerU API → 保存图片 → 组装结果。"""
        async with sem:
            # 用 TemporaryDirectory 包住单页 PDF，确保 finally 清理
            with TemporaryDirectory() as tmpdir:
                tmpdir_path = Path(tmpdir)
                single_pdf_path = tmpdir_path / f"page_{page_num}.pdf"

                # 拆出单页 PDF
                writer = PdfWriter()
                writer.add_page(reader.pages[page_num - 1])
                with open(single_pdf_path, "wb") as f:
                    writer.write(f)

                # 调用 MinerU API（阻塞 → 线程池）
                try:
                    if MINERU_MODE == "flash":
                        result = await loop.run_in_executor(
                            executor,
                            lambda: client.flash_extract(
                                str(single_pdf_path),
                                language=MINERU_LANGUAGE,
                                timeout=MINERU_TIMEOUT,
                            ),
                        )
                    else:
                        result = await loop.run_in_executor(
                            executor,
                            lambda: client.extract(
                                str(single_pdf_path),
                                language=MINERU_LANGUAGE,
                                timeout=MINERU_TIMEOUT,
                                formula=True,
                                table=True,
                            ),
                        )
                except Exception as e:
                    errors.append((page_num, str(e)))
                    logger.error(f"【scan_pdf】第{page_num}页 MinerU API 异常: {e}")
                    return

            # tmpdir 在这里被清理（with 块结束），但 result 数据已拿到

        if result.state != "done":
            err_msg = f"state={result.state}, error={result.error}"
            errors.append((page_num, err_msg))
            logger.error(f"【scan_pdf】第{page_num}页 MinerU 未完成: {err_msg}")
            return

        markdown = result.markdown or ""
        images = result.images or []

        # ── 保存 MinerU 图片 ──
        image_paths: list[str] = []       # metadata.image_paths (兼容现有流程)
        mineru_images: list[dict] = []    # metadata.mineru_images (精确溯源)
        image_map: dict[str, str] = {}    # 原始路径 → 本地文件名

        for idx, img in enumerate(images):
            try:
                # 确定扩展名
                ext = img.name.rsplit(".", 1)[-1] if "." in img.name else "png"
                local_name = f"p{page_num}_i{idx}.{ext}"
                img_full_path = mineru_img_dir / local_name
                img_full_path.write_bytes(img.data)

                # 记录路径 (相对于 data/)
                relative = img_full_path.relative_to(get_data_path()).as_posix()
                image_paths.append(relative)

                mineru_images.append({
                    "name": img.name,
                    "path": relative,
                    "idx": idx,
                    "original_ref": img.path,
                })

                # 建立原始引用 → 本地文件名映射
                if img.path:
                    image_map[img.path] = local_name

            except OSError as e:
                logger.warning(f"【scan_pdf】第{page_num}页图片{idx}保存失败: {e}")
                continue

        # ── 替换 markdown 图片引用 ──
        if image_map:
            markdown = _replace_markdown_image_refs(
                markdown, image_map, user_id, md5_hex,
            )

        # ── 合并 page_image_map 中的图片（PyMuPDF提取的内嵌图） ──
        embedded_images = page_image_map.get(page_num, [])
        all_image_paths = embedded_images + image_paths

        # ── 构建 metadata ──
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
        if mineru_images:
            meta["mineru_images"] = mineru_images

        # ── 逐页日志 ──
        if markdown.strip():
            logger.info(
                f"【scan_pdf】第{page_num}页 MinerU 成功"
                + (f", 图片={len(image_paths)}" if image_paths else "")
            )
        else:
            logger.warning(f"【scan_pdf】第{page_num}页 MinerU 无结果")

        results_by_page[page_num] = {
            "markdown": markdown,
            "metadata": meta,
        }

        if progress_callback:
            done = len(results_by_page) + len(errors)
            await progress_callback(
                "loading",
                f"MinerU 解析中 ({done}/{len(pages_to_process)} 页)...",
            )

    # ── 并发执行所有页面 ──
    tasks = [_process_one_page(pn) for pn in pages_to_process]
    await asyncio.gather(*tasks, return_exceptions=False)

    executor.shutdown(wait=True)

    # ── 组装 Document 列表（按页码排序） ──
    processed = []
    for page_num in sorted(results_by_page.keys()):
        data = results_by_page[page_num]
        doc = Document(
            page_content=data["markdown"].strip(),
            metadata=data["metadata"],
        )
        processed.append(doc)

    # ── 错误处理 ──
    if errors:
        error_detail = ", ".join(
            f"第{pn}页: {msg[:80]}" for pn, msg in errors[:5]
        )
        if len(errors) > 5:
            error_detail += f"... 共{len(errors)}页失败"
        logger.error(f"【scan_pdf】部分页面失败: {error_detail}")

    if not processed:
        raise ValueError(
            f"【scan_pdf】MinerU 解析结果为空: {pdf_name}"
            + (f", 错误: {error_detail}" if errors else "")
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
        f"【scan_pdf】MinerU 完成: {len(processed)}/{len(pages_to_process)} 页, "
        f"图片={total_images} "
        f"(mode={MINERU_MODE}, language={MINERU_LANGUAGE})"
    )

    return processed, {}
