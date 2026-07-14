"""PDF 多模态三分支解析 — 本地处理（无云端依赖）。

分支1 text_pdf:    pdfplumber 直接提取，零视觉/GPU
分支2 text_mix_pdf: pdfplumber 文本+bbox → PyMuPDF 裁切 → 阿里云多模态 VL
分支3 scan_pdf:    MinerU (langchain-mineru) 云端解析
"""
import asyncio
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Awaitable

from langchain_core.documents import Document

from app.config.loader import get_config
from app.utils.log_tool import get_logger

logger = get_logger(__name__)

# 阶段1 并发数: 环境变量 PHASE1_MAX_WORKERS > chroma.yaml phase1_max_workers > CPU×2
_phase1_cfg = int(os.getenv("PHASE1_MAX_WORKERS",
    str(get_config("phase1_max_workers", 0))))
PHASE1_MAX_WORKERS = _phase1_cfg if _phase1_cfg > 0 else (os.cpu_count() or 4) * 2
DEDUP_HAMMING = get_config("dedup_hamming_distance", 10)
VL_INCLUDE_EMBEDDED = get_config("vl_include_embedded_images", False)
CHART_AREA_THRESHOLD = get_config("chart_area_threshold", 5000)
CHART_MAX_CROPS = get_config("chart_max_crops_per_page", 5)
# 图层判定: 忽略小于此尺寸的图片（logo/占位符/追踪像素）
CLASSIFY_IMAGE_MIN_W = get_config("classify_image_min_w", 30)
CLASSIFY_IMAGE_MIN_H = get_config("classify_image_min_h", 30)
RENDER_RESOLUTION = get_config("render_resolution", 2)


# ============================================================
# 图层判定
# ============================================================

def judge_pdf_type(pdf_path: str) -> dict:
    """pdfplumber + PyMuPDF 联合判定 PDF 类型。

    Returns:
        {"pdf_type": "text_pdf"|"text_mix_pdf"|"scan_pdf"|"mixed",
         "page_types": [per-page type list],
         "total_page": int}
    """
    page_types = []
    total_page = 0

    try:
        import fitz
        doc = fitz.open(pdf_path)
        total_page = len(doc)

        for page_num in range(1, total_page + 1):
            page = doc[page_num - 1]
            has_text = len(page.get_text().strip()) > 0
            # 过滤微小图片（logo/占位符/追踪像素，不改变页面类型判定）
            significant_images = 0
            for img in page.get_images():
                try:
                    xref = img[0]
                    info = doc.extract_image(xref)
                    w, h = info.get("width", 0), info.get("height", 0)
                    if w > CLASSIFY_IMAGE_MIN_W and h > CLASSIFY_IMAGE_MIN_H:
                        significant_images += 1
                except Exception:
                    significant_images += 1  # 无法获取尺寸假设为有效图片
            has_image = significant_images > 0

            if has_text and has_image:
                page_types.append("text_mix_pdf")
            elif has_image and not has_text:
                page_types.append("scan_pdf")
            else:
                page_types.append("text_pdf")

        doc.close()
    except Exception as e:
        logger.error(f"【图层判定】失败: {e}")
        total_page = _count_pages_plumber(pdf_path) if total_page == 0 else total_page

    if total_page == 0:
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                total_page = len(pdf.pages)
            page_types = ["text_pdf"] * total_page
        except Exception:
            total_page = 0

    unique_types = set(page_types)
    if len(unique_types) == 1:
        pdf_type = unique_types.pop()
    elif len(unique_types) > 1:
        pdf_type = "mixed"
    else:
        pdf_type = "text_pdf"

    # 逐页 + 汇总日志
    from collections import Counter
    type_counts = Counter(page_types)
    type_summary = ", ".join(f"{t}={c}" for t, c in sorted(type_counts.items()))
    logger.info(f"【图层判定】结果: {pdf_type} ({type_summary}), {total_page} 页")
    if pdf_type == "mixed":
        for i, pt in enumerate(page_types, start=1):
            logger.info(f"【图层判定】  第{i}页 → {pt}")

    return {"pdf_type": pdf_type, "page_types": page_types, "total_page": total_page}


def _count_pages_plumber(pdf_path: str) -> int:
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            return len(pdf.pages)
    except Exception:
        return 0


# ============================================================
# pHash 去重
# ============================================================

def _global_phash_dedup(
    candidates: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """全局 pHash 去重（跨页），汉明距离 ≤ DEDUP_HAMMING 归为一组。

    Args:
        candidates: [(image_path, filename_key), ...] filename_key 用于缓存映射

    Returns:
        (unique_candidates, path_mapping): unique 是去重后的列表，
        path_mapping 将原始路径映射到代表路径（重复路径 → 代表路径）
    """
    if len(candidates) <= 1:
        return candidates, {}

    try:
        import imagehash
        from PIL import Image

        hashes = []
        for p, _ in candidates:
            try:
                hashes.append(imagehash.phash(Image.open(p)))
            except Exception:
                hashes.append(None)

        rep_to_path: dict[str, str] = {}  # 代表路径
        seen_groups = []
        unique = []

        for (p, key), h in zip(candidates, hashes):
            if h is None:
                unique.append((p, key))
                continue
            grouped = False
            for gi, g in enumerate(seen_groups):
                if h - g <= DEDUP_HAMMING:
                    rep_to_path[p] = unique[gi][0]
                    grouped = True
                    break
            if not grouped:
                seen_groups.append(h)
                unique.append((p, key))

        total = len(candidates)
        skipped = total - len(unique)
        if skipped > 0:
            logger.info(f"【全局去重】{total} → {len(unique)} (跨页跳过 {skipped} 重复)")
        return unique, rep_to_path
    except ImportError:
        logger.warning("【全局去重】imagehash/Pillow 未安装，跳过")
        return candidates, {}


# ============================================================
# 分支1: 纯文本 PDF
# ============================================================

def _process_text_pdf(pdf_path: str, file_path: str,
                      page_filter: set | None = None) -> tuple[list[Document], dict]:
    """pdfplumber 直接提取全文，零视觉推理。

    page_filter: 仅处理指定页码集合，None 表示全部页面。
    """
    documents = []
    pdfplumber_ok = 0
    fallback_pymupdf = 0
    try:
        import pdfplumber
        import fitz
        doc_fitz = fitz.open(pdf_path)
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page_num, page in enumerate(pdf.pages, start=1):
                    if page_filter is not None and page_num not in page_filter:
                        continue
                    pdf_ok = True
                    try:
                        text = page.extract_text()
                    except Exception:
                        pdf_ok = False
                        text = doc_fitz[page_num - 1].get_text()
                    if pdf_ok:
                        pdfplumber_ok += 1
                    else:
                        fallback_pymupdf += 1
                    if not text or not text.strip():
                        raise ValueError(
                            f"【text_pdf】第{page_num}页文本提取失败"
                            f"（pdfplumber + PyMuPDF 均无法提取），"
                            f"请检查文件后重新上传: {Path(pdf_path).name}"
                        )
                    doc = Document(
                        page_content=text.strip(),
                        metadata={
                            "source": file_path,
                            "page": page_num,
                            "has_images": False,
                            "toc": "[]",
                            "chapter_count": 0,
                        },
                    )
                    documents.append(doc)
        finally:
            doc_fitz.close()
    except ImportError:
        raise ImportError("pdfplumber 未安装，无法解析纯文本 PDF")
    except Exception as e:
        logger.error(f"【text_pdf】pdfplumber 提取失败: {e}")
        raise

    if not documents:
        raise ValueError(f"PDF 文本提取结果为空: {Path(pdf_path).name}")

    total = len(documents)  # 成功页数（失败由异常提前终止）
    logger.info(
        f"【text_pdf-阶段1】完成: {total} 页, 失败 0 | "
        f"文本: pdfplumber={pdfplumber_ok}, 降级PyMuPDF={fallback_pymupdf}"
    )
    return documents, {}


# ============================================================
# 分支2: 文本+矢量图混合 PDF
# ============================================================

async def _process_text_mix_pdf(
    pdf_path: str,
    file_path: str,
    page_image_map: dict[int, list[str]],
    user_id: str = "",
    md5_hex: str = "",
    progress_callback=None,
    page_filter: set | None = None,
) -> tuple[list[Document], dict]:
    """pdfplumber 文本+表格+bbox → PyMuPDF 裁切矢量图 → 多模态 VL 解读。

    page_filter: 仅处理指定页码集合，None 表示全部页面。
    """
    try:
        import fitz
        import pdfplumber
    except ImportError as e:
        raise ImportError(f"必要依赖未安装: {e}")

    from app.utils.vision_service import get_vision_service

    # 持久化缓存目录: data/extracted_images/{user_id}/{md5}/_vl_cache/
    cache_dir = ""
    if user_id and md5_hex:
        from app.utils.path_tool import get_image_dir
        cache_dir = str(get_image_dir(f"{user_id}/{md5_hex}/_vl_cache"))

    doc_fitz = fitz.open(pdf_path)
    crop_dirs: set[Path] = set()

    # === 阶段1: 并发逐页采集（文字/表格/图片裁切），不做 VL 调用 ===
    # pdfplumber 不是线程安全：多个线程同时操作同一个 PDF 会话的不同 page 对象
    # 会导致底层 pdfminer.six 解析器状态冲突 → 约 25% 提取失败。
    # plumber_lock 串行化所有 pdfplumber 操作，PyMuPDF 裁切在锁外执行。
    pages_data: list[dict] = []
    all_candidates: list[tuple[str, str]] = []
    _phase1_stats = {
        "pages_ok": 0, "pages_pdfplumber_ok": 0, "pages_fallback": 0,
        "persisted_images": 0, "crop_images": 0,
    }
    data_lock = threading.Lock()
    plumber_lock = threading.Lock()
    sem = asyncio.Semaphore(PHASE1_MAX_WORKERS)
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=PHASE1_MAX_WORKERS)

    with pdfplumber.open(pdf_path) as pdf:
        page_list = [
            (page_num, pdf.pages[page_num - 1])
            for page_num in range(1, len(pdf.pages) + 1)
            if page_filter is None or page_num in page_filter
        ]

        async def _process_one_page(page_num: int, plumber_page) -> str:
            async with sem:
                def _work():
                    # ---- pdfplumber 操作（串行化，避免 pdfminer 状态冲突） ----
                    pdfplumber_ok = True
                    with plumber_lock:
                        try:
                            text = plumber_page.extract_text() or ""
                        except Exception:
                            pdfplumber_ok = False
                            text = doc_fitz[page_num - 1].get_text()
                        if not text or not text.strip():
                            raise RuntimeError(f"TEXT_FAIL:第{page_num}页")
                        try:
                            tables = plumber_page.extract_tables() or []
                        except Exception:
                            tables = []
                        # rects 也在锁内提取（底层同样走 pdfminer 解析器）
                        try:
                            rects = plumber_page.rects or []
                        except Exception:
                            rects = []

                    # ---- PyMuPDF 操作（线程安全，在锁外执行以保持并发） ----
                    persisted_images = page_image_map.get(page_num, [])
                    table_texts = []
                    for table in tables:
                        rows = [" | ".join(str(c or "") for c in row) for row in table]
                        table_texts.append("\n".join(rows))
                    crop_images = _crop_chart_regions_from_rects(
                        doc_fitz, rects, page_num, pdf_path
                    )
                    vl_sources = crop_images + persisted_images if VL_INCLUDE_EMBEDDED else crop_images
                    page_candidates = [
                        (p, Path(p).parent.name + "/" + Path(p).name)
                        for p in vl_sources
                    ]
                    return (text, table_texts, crop_images, page_candidates,
                            persisted_images, pdfplumber_ok, len(persisted_images), len(crop_images))

                (text, table_texts, crop_images, page_candidates,
                 persisted_images, pdfplumber_ok, n_persisted, n_crops) = \
                    await loop.run_in_executor(executor, _work)

                with data_lock:
                    if crop_images:
                        crop_dirs.add(Path(crop_images[0]).parent)
                    pages_data.append({
                        "page_num": page_num, "text": text,
                        "table_texts": table_texts, "persisted_images": persisted_images,
                        "vl_candidates": page_candidates,
                        "pdfplumber_ok": pdfplumber_ok,
                    })
                    all_candidates.extend(page_candidates)
                    # 阶段1 统计
                    _stats = _phase1_stats  # mutable dict from closure
                    _stats["pages_ok"] += 1
                    _stats["pages_pdfplumber_ok"] += 1 if pdfplumber_ok else 0
                    _stats["pages_fallback"] += 0 if pdfplumber_ok else 1
                    _stats["persisted_images"] += n_persisted
                    _stats["crop_images"] += n_crops

                if progress_callback:
                    done = len(pages_data)
                    total_pages = len(pdf.pages)  # PDF 总页数
                    await progress_callback("loading",
                        f"图文采集 ({done}/{total_pages})...")
                return text

        results = await asyncio.gather(
            *[_process_one_page(pn, pp) for pn, pp in page_list],
            return_exceptions=True,
        )
        # 文本提取失败 → 立即终止，不允许缺失文本页入库
        for (pn, _), r in zip(page_list, results):
            if isinstance(r, BaseException):
                err_msg = str(r)
                executor.shutdown(wait=True)
                doc_fitz.close()
                for d in crop_dirs:
                    try:
                        shutil.rmtree(str(d), ignore_errors=True)
                    except Exception:
                        pass
                raise ValueError(
                    f"【text_mix_pdf】第{pn}页采集异常: {err_msg}. "
                    f"文本提取失败（pdfplumber + PyMuPDF 均无法提取），"
                    f"请检查文件后重新上传: {Path(pdf_path).name}"
                ) from r
    executor.shutdown(wait=True)
    doc_fitz.close()

    # 阶段1 统计日志
    failed = sum(1 for r in results if isinstance(r, BaseException))
    st = _phase1_stats
    logger.info(
        f"【text_mix_pdf-阶段1】完成: {st['pages_ok']}/{len(page_list)} 页, "
        f"失败 {failed} 页 | "
        f"文本: pdfplumber={st['pages_pdfplumber_ok']}, 降级PyMuPDF={st['pages_fallback']} | "
        f"图片: 嵌入={st['persisted_images']}, 裁切={st['crop_images']}"
    )

    # 恢复页码排序
    pages_data.sort(key=lambda d: d["page_num"])

    # === 阶段2: 全局 pHash 去重 + 查缓存 + 一次并发 VL 调用 ===
    vl_cache = _load_vl_cache(cache_dir) if cache_dir else {}

    uncached: list[tuple[str, str]] = []
    path_to_desc: dict[str, str] = {}
    for path, key in all_candidates:
        if key in vl_cache:
            path_to_desc[path] = vl_cache[key]
        else:
            uncached.append((path, key))

    cache_hits = len(all_candidates) - len(uncached)
    if cache_hits > 0:
        logger.info(f"【VL缓存】命中 {cache_hits} 张，剩余 {len(uncached)} 张需调用")

    new_calls: list[str] = []
    vl_degraded = 0
    if uncached:
        unique_uncached, dedup_map = _global_phash_dedup(uncached)
        new_calls = [p for p, _ in unique_uncached]

        desc_map: dict[str, str] = {}
        if new_calls and VL_INCLUDE_EMBEDDED:
            vs = get_vision_service()
            batch_result = await vs.describe_image_batch(new_calls)
            desc_map = batch_result["results"]
            vl_degraded = batch_result.get("degraded", 0)
        elif new_calls and not VL_INCLUDE_EMBEDDED:
            logger.info(f"【VL】VL_INCLUDE_EMBEDDED=false, 跳过 {len(new_calls)} 张 VL 描述")

        for dup_path, rep_path in dedup_map.items():
            if rep_path in desc_map:
                desc_map[dup_path] = desc_map[rep_path]

        path_to_desc.update(desc_map)

        if cache_dir and any(desc_map.values()):
            _save_vl_cache(cache_dir, desc_map)

    new_desc = len([d for d in path_to_desc.values() if d])

    # === 阶段3: 逐页组装最终 Document ===
    degraded_pages = 0
    documents = []
    for pd_entry in pages_data:
        text = pd_entry["text"]
        table_texts = pd_entry["table_texts"]
        persisted_images = pd_entry["persisted_images"]
        page_num = pd_entry["page_num"]

        vl_descs = []
        page_degraded_images = 0
        for path, _ in pd_entry["vl_candidates"]:
            desc = path_to_desc.get(path, "")
            if desc:
                vl_descs.append(desc)
            else:
                page_degraded_images += 1

        vl_text = ""
        if vl_descs:
            vl_text = "\n\n[图表描述]: " + "\n".join(vl_descs)

        parts = [text]
        if table_texts:
            parts.append("\n\n".join(table_texts))
        if vl_text:
            parts.append(vl_text)

        content = "\n\n".join(p for p in parts if p.strip())
        if not content.strip():
            continue

        page_degraded = page_degraded_images > 0
        if page_degraded:
            degraded_pages += 1

        meta = {
            "source": file_path,
            "page": page_num,
            "has_images": len(persisted_images) > 0,
            "toc": "[]",
            "chapter_count": 0,
        }
        if persisted_images:
            meta["image_paths"] = persisted_images
        if page_degraded:
            meta["degraded"] = True
            meta["degraded_images"] = page_degraded_images
        doc = Document(page_content=content, metadata=meta)
        documents.append(doc)

    if not documents:
        raise ValueError(f"PDF 混合解析结果为空: {Path(pdf_path).name}")

    degradation = {}
    if vl_degraded > 0:
        degradation["vl_timeouts"] = vl_degraded
    if degraded_pages > 0:
        degradation["degraded_pages"] = degraded_pages

    logger.info(
        f"【text_mix_pdf】完成: {len(documents)} 页, "
        f"VL图片={len(all_candidates)}, 缓存命中={cache_hits}, "
        f"去重后调用={len(new_calls)}, 有效描述={new_desc}"
        + (f", 降级={degraded_pages}页" if degraded_pages > 0 else "")
    )
    # 清理裁切临时目录
    for d in crop_dirs:
        try:
            shutil.rmtree(str(d), ignore_errors=True)
        except Exception:
            pass
    return documents, degradation


def _save_vl_cache(cache_dir: str, desc_map: dict[str, str]):
    """持久化 filename→VL描述 映射到缓存目录，供后续复用。"""
    if not desc_map:
        return
    try:
        import json
        cache_path = Path(cache_dir)
        cache_path.mkdir(parents=True, exist_ok=True)
        cache_file = cache_path / "vl_descriptions.json"
        existing = {}
        if cache_file.exists():
            existing = json.loads(cache_file.read_text(encoding="utf-8"))
        for path, desc in desc_map.items():
            if desc:
                p = Path(path)
                existing[p.parent.name + "/" + p.name] = desc
        cache_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"【VL缓存】保存 {len([d for d in desc_map.values() if d])} 条描述 → {cache_file}")
    except Exception:
        pass


def _load_vl_cache(cache_dir: str) -> dict[str, str]:
    """从缓存目录读取 filename→VL描述 映射。"""
    try:
        cache_file = Path(cache_dir) / "vl_descriptions.json"
        if cache_file.exists():
            import json
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            logger.info(f"【VL缓存】命中 {len(data)} 条缓存 → {cache_file}")
            return data
    except Exception:
        pass
    return {}


def _save_pixmap_via_pil(pix, save_path: Path) -> None:
    """PyMuPDF pix.tobytes("png") 失败时的 PIL 兜底保存。"""
    try:
        from PIL import Image
        samples = pix.samples
        width, height = pix.width, pix.height
        n = pix.n
        if n == 4:
            img = Image.frombytes("RGBA", (width, height), bytes(samples))
        elif n == 3:
            img = Image.frombytes("RGB", (width, height), bytes(samples))
        elif n == 1:
            img = Image.frombytes("L", (width, height), bytes(samples))
        else:
            img = Image.frombytes("RGB", (width, height), bytes(samples))
        img.save(str(save_path), format="PNG")
        logger.info(f"【裁切】PIL 兜底保存成功: {save_path.name}")
    except Exception as e:
        logger.warning(f"【裁切】PIL 兜底也失败: {e}")
        raise


def _crop_chart_regions_from_rects(
    doc_fitz, rects: list[dict], page_num: int, pdf_path: str
) -> list[str]:
    """从预提取的 rects 裁切图表区域，保存为临时图片（纯 PyMuPDF 操作，线程安全）。"""
    crops = []
    fitz_page = doc_fitz[page_num - 1]
    chart_rects = [r for r in rects if (r["x1"] - r["x0"]) * (r["y1"] - r["y0"]) > CHART_AREA_THRESHOLD]
    for i, rect in enumerate(chart_rects[:CHART_MAX_CROPS]):
        try:
            clip = fitz.Rect(rect["x0"], rect["y0"], rect["x1"], rect["y1"])
            scale = RENDER_RESOLUTION
            pix = fitz_page.get_pixmap(clip=clip, matrix=fitz.Matrix(scale, scale))
            crop_dir = Path(pdf_path).parent / f"_crops_p{page_num}"
            crop_dir.mkdir(exist_ok=True)
            crop_path = crop_dir / f"crop_{i}.png"
            try:
                crop_path.write_bytes(pix.tobytes("png"))
            except Exception:
                _save_pixmap_via_pil(pix, crop_path)
            crops.append(str(crop_path))
        except Exception:
            continue
    return crops


def _crop_chart_regions(
    doc_fitz, plumber_page, page_num: int, pdf_path: str
) -> list[str]:
    """从页面裁切图表/矢量图区域，保存为临时图片。失败时抛异常触发解析失败流程。"""
    rects = plumber_page.rects or []
    return _crop_chart_regions_from_rects(doc_fitz, rects, page_num, pdf_path)


# ============================================================
# 分支3: 扫描 PDF — MinerU (langchain-mineru)
# ============================================================

async def _process_scan_pdf(
    pdf_path: str,
    file_path: str,
    page_image_map: dict[int, list[str]],
    user_id: str = "",
    md5_hex: str = "",
    progress_callback=None,
    page_filter: set | None = None,
) -> tuple[list[Document], dict]:
    """MinerU 扫描件解析管线 (替代 PaddleOCR)。

    委托给 app.utils.mineru_scan_loader.process_scan_pdf_mineru()。
    """
    from app.utils.mineru_scan_loader import process_scan_pdf_mineru

    return await process_scan_pdf_mineru(
        pdf_path=pdf_path,
        file_path=file_path,
        page_image_map=page_image_map,
        user_id=user_id,
        md5_hex=md5_hex,
        progress_callback=progress_callback,
        page_filter=page_filter,
    )


# ============================================================
# 统一异步入口
# ============================================================

def _merge_degradation(target: dict, source: dict) -> None:
    """合并降级统计：{key: count} 累加。"""
    for k, v in source.items():
        target[k] = target.get(k, 0) + v


async def load_pdf_async(
    file_path: str,
    user_id: str = "",
    md5_hex: str = "",
    original_filename: str = "",
    progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
) -> tuple[list[Document], dict]:
    """PDF 多模态三分支统一入口（异步）。

    流程: 加密检测 → 图片提取持久化 → 图层判定 → 分支路由
    返回: (documents, degradation) — degradation 为空 dict 表示完美解析
    """
    name = Path(file_path).name

    async def _push(stage: str, text: str):
        if progress_callback:
            await progress_callback(stage, text)

    # 0. 加密检测
    await _push("checking", "PDF 完整性检测…")
    try:
        import fitz
        tmp_doc = fitz.open(file_path)
        if tmp_doc.is_encrypted:
            if not tmp_doc.authenticate(""):
                tmp_doc.close()
                raise ValueError(f"PDF 已加密，请提供密码: {name}")
        tmp_doc.close()
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"PDF 文件无法打开（可能已损坏）: {e}")

    # 1. 提取内嵌图片并持久化
    await _push("extracting", "提取内嵌图片…")
    from app.utils.image_extractor import extract_images_from_pdf
    page_image_map = extract_images_from_pdf(file_path, user_id, md5_hex)

    # 2. 图层判定
    await _push("classifying", "判定 PDF 图层类型…")
    info = judge_pdf_type(file_path)
    pdf_type = info["pdf_type"]
    total_page = info["total_page"]

    # 3. 按类型分支处理
    page_types = info.get("page_types", [])
    loop = asyncio.get_running_loop()

    degradation: dict[str, int] = {}

    if pdf_type == "mixed":
        await _push("loading", f"混合类型逐页解析中 ({total_page} 页)…")

        groups: dict[str, set[int]] = {}
        for i, pt in enumerate(page_types, start=1):
            groups.setdefault(pt, set()).add(i)

        all_docs = []
        for pt, page_nums in groups.items():
            if pt == "text_pdf":
                docs, _ = await loop.run_in_executor(
                    None, _process_text_pdf, file_path, file_path, page_nums
                )
            elif pt == "text_mix_pdf":
                docs, d = await _process_text_mix_pdf(
                    file_path, file_path, page_image_map,
                    user_id=user_id, md5_hex=md5_hex,
                    progress_callback=progress_callback, page_filter=page_nums,
                )
                _merge_degradation(degradation, d)
            elif pt == "scan_pdf":
                docs, d = await _process_scan_pdf(
                    file_path, file_path, page_image_map,
                    user_id=user_id, md5_hex=md5_hex,
                    progress_callback=progress_callback, page_filter=page_nums,
                )
                _merge_degradation(degradation, d)
            else:
                continue
            all_docs.extend(docs)

        documents = sorted(all_docs, key=lambda d: d.metadata.get("page", 0))
    elif pdf_type == "text_pdf":
        await _push("loading", f"pdfplumber 提取中 ({total_page} 页)…")
        documents, _ = await loop.run_in_executor(
            None, _process_text_pdf, file_path, file_path
        )
    elif pdf_type == "text_mix_pdf":
        await _push("loading", f"图文混合解析中 ({total_page} 页)…")
        documents, degradation = await _process_text_mix_pdf(
            file_path, file_path, page_image_map,
            user_id=user_id, md5_hex=md5_hex,
            progress_callback=progress_callback,
        )
    elif pdf_type == "scan_pdf":
        await _push("loading", f"扫描件解析中 ({total_page} 页)…")
        documents, degradation = await _process_scan_pdf(
            file_path, file_path, page_image_map,
            user_id=user_id, md5_hex=md5_hex,
            progress_callback=progress_callback,
        )
    else:
        raise ValueError(f"未知 PDF 类型: {pdf_type}")

    logger.info(
        f"【PDF解析】{original_filename or Path(file_path).name}: "
        f"成功 {len(documents)} 页/{total_page} 页, "
        f"降级 {sum(degradation.values()) if degradation else 0} 处, "
        f"类型 {pdf_type}"
    )
    return documents, degradation
