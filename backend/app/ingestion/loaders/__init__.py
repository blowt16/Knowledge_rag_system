"""加载器分发（§3.4.2）。

按格式分发到各自加载器，统一返回 `LoadResult`。
调用方（pipeline）不关心格式差异。
"""

from __future__ import annotations

from pathlib import Path

from app.ingestion.loaders.base import ChapterMark, LoadResult, PageText

__all__ = ["ChapterMark", "LoadResult", "PageText", "load"]


def load(path: Path, *, file_type: str | None = None,
         image_dir: Path | None = None) -> LoadResult:
    """按格式加载文档。

    `file_type` 省略时由内容嗅探决定（不信任扩展名）。
    """
    if file_type is None:
        from app.ingestion.file_type import sniff_format_from_path
        file_type = sniff_format_from_path(path)
        if file_type is None:
            # 嗅探不出（txt/md 无可信签名）→ 退到扩展名
            from app.ingestion.file_type import extension_of
            file_type = extension_of(path.name)

    if file_type == "pdf":
        from app.ingestion.loaders.pdf import load_pdf
        return load_pdf(path, image_dir=image_dir)
    if file_type == "docx":
        from app.ingestion.loaders.docx import load_docx
        return load_docx(path, image_dir=image_dir)
    if file_type == "pptx":
        from app.ingestion.loaders.pptx import load_pptx
        return load_pptx(path, image_dir=image_dir)
    if file_type == "md":
        from app.ingestion.loaders.md import load_md
        return load_md(path)
    if file_type == "txt":
        from app.ingestion.loaders.txt import load_txt
        return load_txt(path)

    raise ValueError(f"不支持的格式：{file_type}（文件 {path.name}）")


def page_boundaries(result: LoadResult, slices: list) -> list[int]:
    """给 chunker 用的「不允许跨越」边界。

    只有 PPTX 需要（chunk 不跨幻灯片）。其余格式返回空列表。
    """
    if len(result.pages) <= 1:
        return []
    return [s.char_start for s in slices[1:]]
