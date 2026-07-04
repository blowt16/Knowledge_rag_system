"""PDF 图片提取 — PyMuPDF 提取内嵌图片并持久化到本地。"""
from pathlib import Path
from app.utils.path_tool import get_data_path
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


def extract_images_from_pdf(pdf_path: str, user_id: str, pdf_md5: str) -> dict[int, list[str]]:
    """从 PDF 提取所有内嵌图片，持久化到 extracted_images/ 目录。

    存储路径: data/extracted_images/{user_id}/{md5}/p{page_num}_i{img_idx}.{ext}

    Returns:
        {页码: [相对图片路径列表]}，路径相对于 data/ 目录
    """
    page_image_map: dict[int, list[str]] = {}

    try:
        import fitz
    except ImportError:
        logger.warning("【图片提取】PyMuPDF (fitz) 未安装，跳过图片提取")
        return page_image_map

    try:
        output_dir = get_data_path(f"extracted_images/{user_id}/{pdf_md5}")
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(f"【图片提取】磁盘不足或权限拒绝，降级为内存模式: {e}")
        return page_image_map

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        logger.error(f"【图片提取】无法打开 PDF: {e}")
        return page_image_map

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
        logger.info(f"【图片提取】共提取 {total} 张图片 → {output_dir}")
    return page_image_map
