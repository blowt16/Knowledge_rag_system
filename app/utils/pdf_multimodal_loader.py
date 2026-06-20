"""PDF 多模态解析 — 纯文本/图文混合/扫描三分支处理。"""
import os
import asyncio
from pathlib import Path
from app.utils.path_tool import get_data_path
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


def judge_pdf_type(pdf_path: str, pdf_md5: str, user_id: str) -> dict:
    """增强图层判定：区分纯文本/图文混合/扫描 PDF，标记需视觉处理的页面。"""
    import fitz

    doc = fitz.open(pdf_path)
    total_page = len(doc)
    pdf_type = "text_pdf"
    vision_need_page_nums = []

    for page_num in range(1, total_page + 1):
        page = doc[page_num - 1]
        objs = page.get_page_objects()
        page_text = page.get_text().strip()
        text_len = len(page_text)
        has_text_obj = any(obj.type == fitz.PDF_OBJECT_TEXT for obj in objs)
        has_image_obj = any(obj.type == fitz.PDF_OBJECT_IMAGE for obj in objs)

        if has_image_obj and text_len < 100:
            vision_need_page_nums.append(page_num)

        if has_text_obj and has_image_obj:
            pdf_type = "mix"
        elif has_image_obj and not has_text_obj:
            pdf_type = "scan_pdf"

    doc.close()

    result = {
        "pdf_type": pdf_type,
        "vision_need_pages": vision_need_page_nums,
        "total_page": total_page,
    }
    logger.info(f"【多模态PDF加载】类型判定: {pdf_type}, 需视觉处理页: {len(vision_need_page_nums)}/{total_page}")
    return result


def _page_phash(page_image) -> str:
    """计算页面的感知哈希。"""
    try:
        import imagehash
        from PIL import Image
        pil_image = Image.fromarray(page_image) if hasattr(page_image, '__array__') else page_image
        return str(imagehash.phash(pil_image))
    except Exception:
        return ""


def _dedup_pages(page_hashes: dict[int, str]) -> dict[int, int]:
    """按 pHash 汉明距离 ≤10 去重，返回 {page_num: representative_page_num}。"""
    groups = {}
    page_mapping = {}

    for page_num, phash in page_hashes.items():
        if not phash:
            page_mapping[page_num] = page_num
            continue
        matched = False
        for rep_page, (rep_hash, members) in groups.items():
            try:
                dist = imagehash.hex_to_hash(phash) - imagehash.hex_to_hash(rep_hash)
                if dist <= 10:
                    members.append(page_num)
                    page_mapping[page_num] = rep_page
                    matched = True
                    break
            except Exception:
                pass
        if not matched:
            groups[page_num] = (phash, [page_num])
            page_mapping[page_num] = page_num

    return page_mapping


async def process_text_pdf(pdf_path: str) -> list[dict]:
    """分支1：纯文本 PDF — PyMuPDF 提取 → pdfplumber 兜底。"""
    import fitz

    blocks = []
    try:
        doc = fitz.open(pdf_path)
        for page_num in range(1, len(doc) + 1):
            page = doc[page_num - 1]
            text = page.get_text().strip()
            if text:
                blocks.append({
                    "page_num": page_num,
                    "block_type": "text",
                    "content": text,
                    "metadata": {},
                })
        doc.close()

        if blocks:
            logger.info(f"【多模态PDF加载】纯文本解析完成: {len(blocks)} 页")
            return blocks
    except Exception as e:
        logger.warning(f"【多模态PDF加载】PyMuPDF 提取失败: {e}，尝试 pdfplumber 兜底")

    # pdfplumber 兜底
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                text = page.extract_text()
                if text and text.strip():
                    blocks.append({
                        "page_num": page_num,
                        "block_type": "text",
                        "content": text.strip(),
                        "metadata": {},
                    })
        logger.info(f"【多模态PDF加载】pdfplumber 兜底完成: {len(blocks)} 页")
    except Exception as e:
        logger.error(f"【多模态PDF加载】pdfplumber 也失败: {e}")

    return blocks


async def process_mix_pdf(pdf_path: str, user_id: str, pdf_md5: str,
                          vision_need_pages: list[int], page_image_map: dict) -> list[dict]:
    """分支2：图文混合 PDF — pdfplumber 提取正文 + 多模态识别图表。"""
    import fitz
    blocks = []

    try:
        doc = fitz.open(pdf_path)

        vision_descriptions = {}
        if vision_need_pages:
            from app.utils.vision_service import VisionService
            vision_svc = VisionService()

            image_tasks = []
            for page_num in vision_need_pages:
                page_images = page_image_map.get(page_num, [])
                for img_path in page_images:
                    abs_path = get_data_path(img_path)
                    if abs_path.exists():
                        image_tasks.append((page_num, str(abs_path)))

            if image_tasks:
                paths = [p for _, p in image_tasks]
                batch_size = int(os.getenv("VISION_BATCH_SIZE", "5"))
                descriptions = await vision_svc.describe_image_batch(paths, batch_size)
                for (page_num, path), desc in zip(image_tasks, descriptions.values()):
                    if desc:
                        key = f"p{page_num}_vision"
                        vision_descriptions[key] = vision_descriptions.get(key, "") + desc + "\n"

        for page_num in range(1, len(doc) + 1):
            page = doc[page_num - 1]
            text = page.get_text().strip()
            vision_key = f"p{page_num}_vision"
            content = text
            if vision_key in vision_descriptions:
                content = f"{text}\n\n[页面视觉描述]: {vision_descriptions[vision_key]}"

            if content.strip():
                blocks.append({
                    "page_num": page_num,
                    "block_type": "mix",
                    "content": content,
                    "metadata": {"has_images": page_num in vision_need_pages},
                })

        doc.close()
        logger.info(f"【多模态PDF加载】图文混合解析完成: {len(blocks)} 页")
    except Exception as e:
        logger.error(f"【多模态PDF加载】图文混合解析失败: {e}")

    return blocks


async def process_scan_pdf(pdf_path: str, user_id: str, pdf_md5: str,
                           page_image_map: dict) -> list[dict]:
    """分支3：扫描 PDF — 144dpi 渲染整页 → OpenCV 预处理 → 多模态整页识别。"""
    import fitz
    import numpy as np

    blocks = []
    try:
        doc = fitz.open(pdf_path)
        dedup_enabled = os.getenv("VISION_DEDUP_ENABLED", "true").lower() == "true"

        # 计算页面哈希
        page_hashes = {}
        for page_num in range(1, len(doc) + 1):
            page = doc[page_num - 1]
            mat = fitz.Matrix(2, 2)  # 144 dpi
            pix = page.get_pixmap(matrix=mat)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            if dedup_enabled:
                page_hashes[page_num] = _page_phash(img)

        # 去重映射
        page_mapping = _dedup_pages(page_hashes) if dedup_enabled else {p: p for p in range(1, len(doc) + 1)}

        # 收集代表页
        unique_pages = set(page_mapping.values())
        from app.utils.vision_service import VisionService
        vision_svc = VisionService()
        batch_size = int(os.getenv("VISION_BATCH_SIZE", "5"))

        # 渲染并描述代表页
        page_descriptions = {}
        unique_list = sorted(unique_pages)
        for i in range(0, len(unique_list), batch_size):
            batch = unique_list[i:i + batch_size]
            tasks = []
            temp_paths = []
            for page_num in batch:
                page = doc[page_num - 1]
                mat = fitz.Matrix(2, 2)
                pix = page.get_pixmap(matrix=mat)
                temp_path = get_data_path(f"extracted_images/{user_id}/{pdf_md5}/_scan_p{page_num}.png")
                temp_path.parent.mkdir(parents=True, exist_ok=True)
                pix.save(str(temp_path))
                temp_paths.append(str(temp_path))
                tasks.append(str(temp_path))

            descs = await vision_svc.describe_image_batch(tasks, batch_size)
            for page_num, path in zip(batch, temp_paths):
                desc = descs.get(path, "")
                page_descriptions[page_num] = desc if desc else "[本页扫描图像识别失败]"

        # 组装结果（复用去重映射）
        for page_num in range(1, len(doc) + 1):
            rep = page_mapping.get(page_num, page_num)
            desc = page_descriptions.get(rep, "[本页扫描图像识别失败]")
            blocks.append({
                "page_num": page_num,
                "block_type": "scan",
                "content": desc if desc.strip() else "[本页扫描图像识别失败]",
                "metadata": {"dedup_representative": rep if rep != page_num else None},
            })

        doc.close()
        logger.info(f"【多模态PDF加载】扫描件解析完成: {len(blocks)} 页")
    except Exception as e:
        logger.error(f"【多模态PDF加载】扫描件解析失败: {e}")

    return blocks


async def pdf_multimodal_loader(pdf_path: str, user_id: str, pdf_md5: str) -> list[dict]:
    """PDF 多模态加载主入口 — 三分支并行处理。

    Returns:
        list[dict]: 每页结构化描述 [{page_num, block_type, content, metadata}, ...]
    """
    from app.utils.image_extractor import extract_images_from_pdf

    # 1. 提取图片
    page_image_map = extract_images_from_pdf(pdf_path, user_id, pdf_md5)

    # 2. 判定 PDF 类型
    pdf_info = judge_pdf_type(pdf_path, pdf_md5, user_id)
    pdf_type = pdf_info["pdf_type"]
    vision_need_pages = pdf_info["vision_need_pages"]

    # 3. 按类型走分支
    if pdf_type == "text_pdf":
        return await process_text_pdf(pdf_path)
    elif pdf_type == "mix":
        return await process_mix_pdf(pdf_path, user_id, pdf_md5, vision_need_pages, page_image_map)
    elif pdf_type == "scan_pdf":
        return await process_scan_pdf(pdf_path, user_id, pdf_md5, page_image_map)
    else:
        logger.warning(f"【多模态PDF加载】未知 PDF 类型: {pdf_type}，按纯文本处理")
        return await process_text_pdf(pdf_path)


def pdf_multimodal_loader_sync(pdf_path: str, user_id: str, pdf_md5: str) -> list[dict]:
    """同步版 PDF 多模态加载（线程池内调用）。"""
    return asyncio.run(pdf_multimodal_loader(pdf_path, user_id, pdf_md5))
