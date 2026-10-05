"""PPTX 加载器（§3.4.2）。

⚠️ **必须输出幻灯片序号**：旧 `pptx_loader` 把整份 PPT 的文本拼成一个字符串、
   没有幻灯片序号 —— 不补这个，pptx 的引用回跳与分块预览都定位不到「第几页」。

⚠️ `page` = **幻灯片序号，从 1 起**，直接复用现有字段，不新增 `slide_index`。

⚠️ **chunk 不跨幻灯片**：按幻灯片边界切分；若单页文本超过 500 字阈值，
   页内再按标点切 —— 但不把两页的内容合进一个 chunk，否则 `page` 无法表达。
   调用方用 `LoadResult.page_boundaries()` 拿到边界传给 chunker。

⚠️ 旧项目 `pptx` 包是坏的（只有 dist-info 没有目录，附录 B.1.1），
   M0 已 `--force-reinstall` 修好并实测 `import pptx` 成功。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from pptx import Presentation

from app.ingestion.loaders.base import LoadResult, PageText


def _slide_text(slide) -> str:
    """取一张幻灯片上的全部文本。

    按 shape 顺序拼接；表格逐行展开 —— 不展开会丢掉整张表。
    """
    parts: list[str] = []
    for shape in slide.shapes:
        if shape.has_text_frame:
            text = shape.text_frame.text.strip()
            if text:
                parts.append(text)
        if getattr(shape, "has_table", False) and shape.has_table:
            for row in shape.table.rows:
                cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
                if any(cells):
                    parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_images(path: Path, out_dir: Path | None) -> dict[int, list[str]]:
    """从 pptx 容器取图，按幻灯片序号归组。

    以 `ppt/media/imageN.ext` 为源；映射回幻灯片靠 `ppt/slides/_rels/*.rels`。
    为避免解析 rels 的复杂度，这里退一步：把图片统一落盘并按文件名返回，
    分派到具体幻灯片的事由引用卡片按 `image_paths` 展示——**不影响检索**。
    """
    if out_dir is None:
        return {}
    saved: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not name.startswith("ppt/media/"):
                    continue
                data = zf.read(name)
                if len(data) < 2048:
                    continue
                out_dir.mkdir(parents=True, exist_ok=True)
                target = out_dir / Path(name).name
                target.write_bytes(data)
                saved.append(target.name)
    except (zipfile.BadZipFile, OSError):
        return {}
    # 全部图片挂在第 1 页的列表里（按 name 展示），避免错配到具体幻灯片
    return {1: saved} if saved else {}


def load_pptx(path: Path, *, image_dir: Path | None = None) -> LoadResult:
    presentation = Presentation(str(path))
    images = _extract_images(path, image_dir)

    result = LoadResult()
    for index, slide in enumerate(presentation.slides):
        page_no = index + 1                      # 幻灯片序号，从 1 起
        text = _slide_text(slide)
        if not text.strip():
            # 空幻灯片也记进 pages，让 missing_pages 能体现出来（B.1.2 同类处理）
            result.pages.append(PageText(page=page_no, text=""))
            continue
        result.pages.append(PageText(
            page=page_no,
            text=text,
            image_paths=images.get(page_no, []),
        ))

    result.missing_pages = [p.page for p in result.pages if not p.text.strip()]
    return result
