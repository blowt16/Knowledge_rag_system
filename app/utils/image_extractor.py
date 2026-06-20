"""PDF 图片提取 — PyMuPDF 提取内嵌图片并持久化。"""
import hashlib
from pathlib import Path
from app.utils.path_tool import get_data_path
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


def extract_images_from_pdf(pdf_path: str, user_id: str, pdf_md5: str) -> dict[int, list[str]]:
    """从 PDF 提取内嵌图片，返回 {页码: [图片路径列表]}。

    存储路径: data/extracted_images/{user_id}/{md5}/p{page_num}_i{img_idx}.{ext}
    """
    page_image_map: dict[int, list[str]] = {}
    try:
        import fitz
    except ImportError:
        logger.warning("【图片提取】PyMuPDF 未安装")
        return page_image_map

    try:
        output_dir = get_data_path(f"extracted_images/{user_id}/{pdf_md5}")
        doc = fitz.open(pdf_path)

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

        doc.close()
        total = sum(len(v) for v in page_image_map.values())
        if total > 0:
            logger.info(f"【图片提取】共提取 {total} 张图片")
        return page_image_map

    except Exception as e:
        logger.error(f"【图片提取】PDF 图片提取失败: {e}")
        return {}
