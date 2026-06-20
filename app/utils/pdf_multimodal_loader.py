"""PDF 多模态解析 — 按报告设计完整实现：加密检测 + 三分支 + OpenCV + bbox + pHash。"""
import os
import asyncio
from app.config.loader import get_config
from app.utils.path_tool import get_data_path
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


# ============================================================
# PDF 加密检测
# ============================================================

def _open_pdf(pdf_path: str):
    """打开 PDF，自动处理加密检测。"""
    import fitz

    doc = fitz.open(pdf_path)
    if doc.needs_pass:
        logger.info(f"【多模态PDF加载】检测到加密 PDF，尝试空密码解密")
        if doc.authenticate(""):
            logger.info("【多模态PDF加载】空密码解密成功")
        else:
            doc.close()
            raise ValueError("PDF 文件已加密，请上传已解密的版本或提供密码后重试")
    return doc


# ============================================================
# 图层判定 + 图片提取
# ============================================================

def judge_pdf_type(pdf_path: str, pdf_md5: str, user_id: str) -> dict:
    """增强图层判定：区分纯文本/图文混合/扫描 PDF，标记需视觉处理的页面。

    Returns:
        {"pdf_type": str, "vision_need_pages": list[int], "total_page": int,
         "page_image_map": dict[int, list[str]]}
    """
    import fitz

    doc = _open_pdf(pdf_path)
    total_page = len(doc)
    pdf_type = "text_pdf"
    vision_need_page_nums = []
    min_text_len = get_config("vision_min_text_length", 100)

    # 提取内嵌图片
    page_image_map = _extract_images(doc, user_id, pdf_md5)

    for page_num in range(1, total_page + 1):
        page = doc[page_num - 1]
        page_text = page.get_text().strip()
        text_len = len(page_text)

        # 检查页面对象类型
        has_text_obj = False
        has_image_obj = False
        try:
            objs = page.get_page_objects() if hasattr(page, 'get_page_objects') else []
            has_text_obj = any(obj.type == fitz.PDF_OBJECT_TEXT for obj in objs)
            has_image_obj = any(obj.type == fitz.PDF_OBJECT_IMAGE for obj in objs)
        except Exception:
            # 降级：通过文本长度和图片判断
            has_text_obj = text_len >= 50
            has_image_obj = page_num in page_image_map

        # 视觉触发规则：页面有图片且文本字符 < min_text_len
        if has_image_obj and text_len < min_text_len:
            vision_need_page_nums.append(page_num)

        # 类型判定
        if has_text_obj and has_image_obj:
            pdf_type = "mix"
        elif has_image_obj and not has_text_obj:
            pdf_type = "scan_pdf"

    doc.close()

    result = {
        "pdf_type": pdf_type,
        "vision_need_pages": vision_need_page_nums,
        "total_page": total_page,
        "page_image_map": page_image_map,
    }
    logger.info(
        f"【多模态PDF加载】类型判定: {pdf_type}, "
        f"需视觉处理页: {len(vision_need_page_nums)}/{total_page}"
    )
    return result


def _extract_images(doc, user_id: str, pdf_md5: str) -> dict[int, list[str]]:
    """PyMuPDF 提取 PDF 内嵌图片。

    Returns:
        {页码: [图片相对路径列表]}
    """
    page_image_map: dict[int, list[str]] = {}
    try:
        output_dir = get_data_path(f"extracted_images/{user_id}/{pdf_md5}")

        for page_num in range(1, len(doc) + 1):
            page = doc[page_num - 1]
            images = page.get_images(full=True)

            for img_idx, img_info in enumerate(images):
                try:
                    xref = img_info[0]
                    base_image = doc.extract_image(xref)
                    image_bytes = base_image["image"]
                    ext = base_image["ext"]

                    img_filename = f"p{page_num}_i{img_idx}.{ext}"
                    img_path = output_dir / img_filename
                    img_path.write_bytes(image_bytes)

                    relative_path = str(img_path.relative_to(get_data_path()))
                    page_image_map.setdefault(page_num, []).append(relative_path)
                except Exception as e:
                    logger.warning(f"【图片提取】第{page_num}页图片{img_idx}提取失败: {e}")
                    continue

        total = sum(len(v) for v in page_image_map.values())
        if total > 0:
            logger.info(f"【图片提取】共提取 {total} 张图片")
    except Exception as e:
        logger.error(f"【图片提取】PDF 图片提取失败: {e}")

    return page_image_map


# ============================================================
# 感知哈希去重
# ============================================================

def _page_phash(page_image) -> str:
    """计算页面的感知哈希。"""
    try:
        import numpy as np
        import imagehash
        from PIL import Image

        if hasattr(page_image, '__array__'):
            pil_image = Image.fromarray(np.asarray(page_image))
        else:
            pil_image = page_image
        return str(imagehash.phash(pil_image))
    except Exception:
        return ""


def _dedup_pages(page_hashes: dict[int, str]) -> dict[int, int]:
    """按 pHash 汉明距离去重，返回 {page_num: representative_page_num}。"""
    import imagehash

    max_dist = get_config("dedup_hamming_distance", 10)
    groups: dict[int, tuple[str, list[int]]] = {}
    page_mapping: dict[int, int] = {}

    for page_num, phash_str in page_hashes.items():
        if not phash_str:
            page_mapping[page_num] = page_num
            continue
        try:
            current_hash = imagehash.hex_to_hash(phash_str)
        except Exception:
            page_mapping[page_num] = page_num
            continue

        matched = False
        for rep_page, (rep_hash_str, members) in groups.items():
            try:
                dist = current_hash - imagehash.hex_to_hash(rep_hash_str)
                if dist <= max_dist:
                    members.append(page_num)
                    page_mapping[page_num] = rep_page
                    matched = True
                    break
            except Exception:
                pass

        if not matched:
            groups[page_num] = (phash_str, [page_num])
            page_mapping[page_num] = page_num

    # 仅一个去重组时关闭去重
    if len(set(page_mapping.values())) <= 1:
        return {p: p for p in page_hashes}

    return page_mapping


# ============================================================
# 分支1：纯文本 PDF
# ============================================================

async def process_text_pdf(pdf_path: str) -> list[dict]:
    """纯文本 PDF — PyMuPDF 提取 → pdfplumber 兜底。"""
    import fitz

    blocks = []
    try:
        doc = _open_pdf(pdf_path)
        for page_num in range(1, len(doc) + 1):
            page = doc[page_num - 1]
            text = page.get_text().strip()
            if text:
                blocks.append({
                    "page_num": page_num,
                    "block_type": "text",
                    "content": text,
                    "bbox": None,
                    "level": "正文",
                    "metadata": {"page_phash": "", "dedup_group_id": None},
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
                        "bbox": None,
                        "level": "正文",
                        "metadata": {"page_phash": "", "dedup_group_id": None},
                    })
        logger.info(f"【多模态PDF加载】pdfplumber 兜底完成: {len(blocks)} 页")
    except Exception as e:
        logger.error(f"【多模态PDF加载】pdfplumber 也失败: {e}")

    return blocks


# ============================================================
# 分支2：图文混合 PDF（pdfplumber + bbox 裁剪 + pHash 去重）
# ============================================================

async def process_mix_pdf(pdf_path: str, user_id: str, pdf_md5: str,
                          vision_need_pages: list[int],
                          page_image_map: dict[int, list[str]]) -> list[dict]:
    """混合 PDF — pdfplumber 正文/表格/图表 + PyMuPDF bbox 裁剪 + 多模态。"""
    import fitz
    import numpy as np

    blocks = []

    try:
        doc = _open_pdf(pdf_path)

        # 1. 渲染视觉页面截图用于 pHash
        scale = float(os.getenv("SCAN_RENDER_SCALE", "2"))
        mat = fitz.Matrix(scale, scale)
        page_hashes = {}
        dedup_enabled = os.getenv("VISION_DEDUP_ENABLED", "true").lower() == "true"

        if dedup_enabled and vision_need_pages:
            for page_num in vision_need_pages:
                page = doc[page_num - 1]
                pix = page.get_pixmap(matrix=mat)
                img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                    pix.height, pix.width, pix.n
                )
                page_hashes[page_num] = _page_phash(img)

            page_mapping = _dedup_pages(page_hashes)
        else:
            page_mapping = {p: p for p in vision_need_pages}

        # 2. pdfplumber 提取正文 + 表格 + 图表 bbox
        with _get_pdfplumber(pdf_path) as pdf:
            for page_num in range(1, len(doc) + 1):
                page = doc[page_num - 1]
                fitz_text = page.get_text().strip()
                fitz_page = page

                # bbox 候选列表
                chart_bboxes = []
                table_texts = []

                try:
                    p_page = pdf.pages[page_num - 1]
                    # 提取正文
                    pdfplumber_text = p_page.extract_text() or ""
                    # 提取表格
                    tables = p_page.extract_tables()
                    for tbl in tables:
                        tbl_text = _format_table(tbl)
                        if tbl_text:
                            table_texts.append(tbl_text)
                    # 提取图表/图片 bbox
                    for obj in p_page.rects + p_page.images if hasattr(p_page, 'rects') else []:
                        if hasattr(obj, 'bbox'):
                            chart_bboxes.append(obj.bbox)
                except Exception:
                    pdfplumber_text = ""

                # 3. 多模态：裁切图表 + 调用多模态
                vision_descriptions = []
                if page_num in vision_need_pages:
                    rep_page = page_mapping.get(page_num, page_num)
                    if rep_page == page_num:
                        # 代表页：裁切并调用多模态
                        for bbox in chart_bboxes:
                            try:
                                clip = fitz.Rect(bbox)
                                pix = fitz_page.get_pixmap(matrix=mat, clip=clip)
                                desc = await _describe_pixmap(pix, user_id, pdf_md5, page_num, "chart")
                                if desc:
                                    vision_descriptions.append(f"[图表描述]: {desc}")
                            except Exception:
                                pass

                        # 如果页面有内嵌图片，也送去识别
                        page_images = page_image_map.get(page_num, [])
                        for img_path in page_images[:3]:  # 最多3张
                            abs_path = get_data_path(img_path)
                            if abs_path.exists():
                                desc = await _describe_image_file(str(abs_path))
                                if desc:
                                    vision_descriptions.append(f"[图片描述]: {desc}")

                        if not vision_descriptions and not fitz_text:
                            # 整页多模态兜底
                            pix = fitz_page.get_pixmap(matrix=mat)
                            desc = await _describe_pixmap(pix, user_id, pdf_md5, page_num, "page")
                            if desc:
                                vision_descriptions.append(f"[页面视觉描述]: {desc}")
                    else:
                        # 复用代表页结果（同组去重）
                        vision_descriptions.append(f"[去重复用第{rep_page}页视觉描述]")

                # 4. 组装内容：按 y 坐标拼接
                content_parts = []
                if fitz_text:
                    content_parts.append(fitz_text)
                for tbl in table_texts:
                    content_parts.append(f"[表格数据]\n{tbl}")
                content_parts.extend(vision_descriptions)

                content = "\n\n".join(content_parts)
                if content.strip():
                    blocks.append({
                        "page_num": page_num,
                        "block_type": "mix",
                        "content": content,
                        "bbox": None,
                        "level": "正文",
                        "metadata": {
                            "has_images": page_num in vision_need_pages,
                            "page_phash": page_hashes.get(page_num, ""),
                            "dedup_group_id": page_mapping.get(page_num),
                            "vision_source": "aliyun_llm" if vision_descriptions else "",
                            "downgrade_flag": False,
                        },
                    })

        doc.close()
        logger.info(f"【多模态PDF加载】图文混合解析完成: {len(blocks)} 页")
    except Exception as e:
        logger.error(f"【多模态PDF加载】图文混合解析失败: {e}")

    return blocks


# ============================================================
# 分支3：扫描 PDF（OpenCV 预处理）
# ============================================================

async def process_scan_pdf(pdf_path: str, user_id: str, pdf_md5: str,
                           page_image_map: dict[int, list[str]]) -> list[dict]:
    """扫描 PDF — 144dpi 渲染 → OpenCV 预处理 → 多模态整页识别。"""
    import fitz
    import numpy as np

    blocks = []
    try:
        doc = _open_pdf(pdf_path)
        scale = float(os.getenv("SCAN_RENDER_SCALE", "2"))
        mat = fitz.Matrix(scale, scale)
        dedup_enabled = os.getenv("VISION_DEDUP_ENABLED", "true").lower() == "true"

        # 1. 渲染 + 计算 pHash
        page_hashes = {}
        for page_num in range(1, len(doc) + 1):
            page = doc[page_num - 1]
            pix = page.get_pixmap(matrix=mat)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, pix.n
            )
            if dedup_enabled:
                page_hashes[page_num] = _page_phash(img)

        # 2. 去重映射
        page_mapping = _dedup_pages(page_hashes) if dedup_enabled else {
            p: p for p in range(1, len(doc) + 1)
        }

        # 3. 代表页：OpenCV 预处理 → 多模态识别
        from app.utils.vision_service import VisionService
        vision_svc = VisionService()
        batch_size = int(os.getenv("VISION_BATCH_SIZE", "5"))
        unique_pages = sorted(set(page_mapping.values()))

        page_descriptions = {}
        for i in range(0, len(unique_pages), batch_size):
            batch = unique_pages[i:i + batch_size]
            tasks = []
            temp_paths = []

            for page_num in batch:
                page = doc[page_num - 1]
                pix = page.get_pixmap(matrix=mat)
                img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                    pix.height, pix.width, pix.n
                )

                # OpenCV 预处理
                processed = _opencv_preprocess(img)
                if processed is not None:
                    img = processed

                temp_path = get_data_path(
                    f"extracted_images/{user_id}/{pdf_md5}/_scan_p{page_num}.png"
                )
                temp_path.parent.mkdir(parents=True, exist_ok=True)
                _save_image(img, str(temp_path))
                temp_paths.append(str(temp_path))
                tasks.append(str(temp_path))

            descs = await vision_svc.describe_image_batch(tasks, batch_size)
            for pg, path in zip(batch, temp_paths):
                page_descriptions[pg] = descs.get(path, "") or "[本页扫描图像识别失败]"

        # 4. 组装结果（复用去重映射）
        for page_num in range(1, len(doc) + 1):
            rep = page_mapping.get(page_num, page_num)
            desc = page_descriptions.get(rep, "[本页扫描图像识别失败]")
            blocks.append({
                "page_num": page_num,
                "block_type": "scan",
                "content": desc if desc.strip() else "[本页扫描图像识别失败]",
                "bbox": None,
                "level": "正文",
                "metadata": {
                    "page_phash": page_hashes.get(page_num, ""),
                    "dedup_group_id": rep if rep != page_num else None,
                    "vision_source": "aliyun_llm",
                    "downgrade_flag": desc == "[本页扫描图像识别失败]",
                },
            })

        doc.close()
        logger.info(f"【多模态PDF加载】扫描件解析完成: {len(blocks)} 页")
    except Exception as e:
        logger.error(f"【多模态PDF加载】扫描件解析失败: {e}")

    return blocks


def _opencv_preprocess(img) -> "np.ndarray | None":
    """OpenCV 预处理：灰度 → 二值化 → 倾斜矫正 → 裁白边 → 降噪。"""
    try:
        import cv2
        import numpy as np

        # 灰度
        if len(img.shape) == 3 and img.shape[2] == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        elif len(img.shape) == 3 and img.shape[2] == 4:
            gray = cv2.cvtColor(img, cv2.COLOR_RGBA2GRAY)
        else:
            gray = img

        # 二值化
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        # 倾斜矫正
        coords = np.column_stack(np.where(binary > 0))
        if len(coords) > 0:
            angle = cv2.minAreaRect(coords)[-1]
            if angle < -45:
                angle = -(90 + angle)
            if abs(angle) > 0.5:
                (h, w) = binary.shape[:2]
                center = (w // 2, h // 2)
                M = cv2.getRotationMatrix2D(center, angle, 1.0)
                binary = cv2.warpAffine(
                    binary, M, (w, h),
                    flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE,
                )

        # 裁白边
        nonzero = cv2.findNonZero(binary)
        if nonzero is not None:
            x, y, w, h = cv2.boundingRect(nonzero)
            binary = binary[y:y + h, x:x + w]

        # 降噪
        denoised = cv2.fastNlMeansDenoising(binary, None, 10, 7, 21)

        return denoised
    except Exception as e:
        logger.warning(f"【OpenCV预处理】失败: {e}")
        return None


# ============================================================
# 工具函数
# ============================================================

def _save_image(img_array, path: str):
    """保存 numpy 数组为图片。"""
    try:
        import cv2
        cv2.imwrite(path, img_array)
    except Exception:
        from PIL import Image
        import numpy as np
        if len(img_array.shape) == 2:
            pil_img = Image.fromarray(img_array, mode='L')
        else:
            pil_img = Image.fromarray(img_array)
        pil_img.save(path)


def _format_table(table: list[list[str | None]]) -> str:
    """格式化表格为文本。"""
    if not table:
        return ""
    rows = []
    for row in table:
        if row:
            cells = [str(c) if c else "" for c in row]
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _get_pdfplumber(pdf_path: str):
    """获取 pdfplumber PDF 对象（上下文管理器）。"""
    import pdfplumber
    return pdfplumber.open(pdf_path)


async def _describe_pixmap(pix, user_id: str, pdf_md5: str,
                           page_num: int, prefix: str = "page") -> str:
    """保存 pixmap 为临时图片并调用多模态描述。"""
    import uuid
    temp_path = get_data_path(
        f"extracted_images/{user_id}/{pdf_md5}/_{prefix}_p{page_num}_{uuid.uuid4().hex[:6]}.png"
    )
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(temp_path))

    result = await _describe_image_file(str(temp_path))
    return result


async def _describe_image_file(image_path: str) -> str:
    """调用视觉服务描述单张图片。"""
    from app.utils.vision_service import VisionService
    vision_svc = VisionService()
    return await vision_svc.describe_image(image_path)


# ============================================================
# 主入口
# ============================================================

async def pdf_multimodal_loader(pdf_path: str, user_id: str, pdf_md5: str) -> list[dict]:
    """PDF 多模态加载主入口 — 按报告设计完整实现。

    Returns:
        list[dict]: 每页结构化描述
        [{page_num, block_type, content, bbox, level, metadata}, ...]
    """
    # 1. 判定 PDF 类型（含图片提取）
    pdf_info = judge_pdf_type(pdf_path, pdf_md5, user_id)
    pdf_type = pdf_info["pdf_type"]
    vision_need_pages = pdf_info["vision_need_pages"]
    page_image_map = pdf_info["page_image_map"]

    # 2. 按类型走分支（与报告描述一致）
    if pdf_type == "text_pdf":
        return await process_text_pdf(pdf_path)
    elif pdf_type == "mix":
        return await process_mix_pdf(
            pdf_path, user_id, pdf_md5, vision_need_pages, page_image_map
        )
    elif pdf_type == "scan_pdf":
        return await process_scan_pdf(pdf_path, user_id, pdf_md5, page_image_map)
    else:
        logger.warning(f"【多模态PDF加载】未知 PDF 类型: {pdf_type}，按纯文本处理")
        return await process_text_pdf(pdf_path)


def pdf_multimodal_loader_sync(pdf_path: str, user_id: str, pdf_md5: str) -> list[dict]:
    """同步版 PDF 多模态加载（线程池内调用）。"""
    return asyncio.run(pdf_multimodal_loader(pdf_path, user_id, pdf_md5))
