"""元数据富化（§3.3.2）。

产出 Chroma chunk metadata 的全部字段。**每一个都有下游消费者**，
不是"先存着"：

| 字段 | 谁在用 |
|---|---|
| `document_id` / `doc_group_id` / `version` | 版本折叠（回查 PG 取组内最大 version） |
| `status` / `effective_date` / `visibility` / `vis_*` | 检索期过滤公式（§3.3.3） |
| `chunk_id` | RRF 去重键、引用、qa_logs、评测集 —— **四处都用它** |
| `char_start` / `char_end` | 亮点③ 的字符偏移（参照系是规范化文本） |
| `current_chapter` / `chapter_level` | 引用溯源到章节 |
| `page` | 引用回跳的页码 |
| `bbox` | **回跳的主定位依据**（坐标高亮，优先于文本匹配） |
| `image_paths` | 引用面板的缩略图 |

⚠️ `vis_admin` / `vis_staff` / `vis_student` 是**三个布尔字段，不是数组**：
   Chroma 的 `where` 只能在标量上做 `$eq`/`$in`，**对 JSON 字符串做不了成员判断**。
   管理端写的是 `documents.visible_roles`（JSONB），进 Chroma 时展开成布尔字段。

⚠️ 偏移的参照系是 `documents.normalized_text_path` 那份**清洗后的规范化文本**，
   **不是原始 PDF 的字节偏移**（清洗会删除页眉页脚，两者对不上）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.core.deps import ALL_ROLES
from app.ingestion.chunker import ChunkSpan, make_chunk_id
from app.ingestion.clean import PageSlice, page_of_offset
from app.ingestion.loaders.base import ChapterMark


@dataclass
class DocumentMeta:
    """documents 表里与 chunk metadata 相关的部分。"""
    id: str
    doc_group_id: str
    title: str
    filename: str
    file_type: str
    version: int
    effective_date: date
    status: str
    visibility: str
    visible_roles: list[str]


def chapter_at(marks: list[ChapterMark], offset: int) -> tuple[str, int]:
    """取该偏移之前**最近的一个**章节标记。

    ⚠️ 这是 B.2.1 / E.3.2 的修复点：
       旧实现把 `doc.metadata["current_chapter"]`（**全文档第一个标题**）
       原样复制到该文件的每一个 chunk —— 一份制度文档的所有片段
       在 prompt 和引用里都显示成「第一章 xxx」。
       而 PDF 更彻底：加载器**从不写这个字段**，实测 1244/1244 全为空串。

       「这比没有章节更糟」—— 没有章节用户知道信息缺失，章节错的会误导用户
       以为找对了地方。亮点③（引用溯源到章节）的可信度直接归零。
    """
    found = ("", 0)
    for mark in marks:
        if mark.char_offset <= offset:
            found = (mark.chapter, mark.level)
        else:
            break
    return found


def boxes_for_range(page_boxes: list[dict], slices: list[PageSlice],
                    char_start: int, char_end: int) -> list[dict]:
    """取覆盖该字符区间的 bbox 框（可跨页）。

    返回 `{"page": n, "x0":…, "x1":…, "top":…, "bottom":…}` 列表，
    供前端 `react-pdf-highlighter` 按坐标高亮 —— **这是回跳的主路径**。
    非 PDF、扫描件、旧索引会为空数组，此时前端退到文本匹配（§4.2.2.4）。
    """
    if not page_boxes:
        return []
    start_page = page_of_offset(slices, char_start)
    end_page = page_of_offset(slices, max(char_start, char_end - 1))

    out: list[dict] = []
    for box in page_boxes:
        page_no = box.get("page")
        if page_no is None:
            continue
        if start_page <= page_no <= end_page:
            out.append({
                "page": page_no,
                "x0": box.get("x0"),
                "x1": box.get("x1"),
                "top": box.get("top"),
                "bottom": box.get("bottom"),
            })
    return out


def build_chunk_metadata(
    *,
    doc: DocumentMeta,
    chunk: ChunkSpan,
    slices: list[PageSlice],
    marks: list[ChapterMark],
    page_boxes: list[dict] | None = None,
    image_by_page: dict[int, list[str]] | None = None,
) -> dict[str, Any]:
    """组装单个 chunk 的 Chroma metadata。"""
    chapter, level = chapter_at(marks, chunk.char_start)
    page = page_of_offset(slices, chunk.char_start)

    images: list[str] = []
    if image_by_page:
        images = list(image_by_page.get(page, []))

    roles = set(doc.visible_roles or [])

    meta: dict[str, Any] = {
        "document_id": doc.id,
        "doc_group_id": doc.doc_group_id,
        "version": int(doc.version),
        "status": doc.status,
        "effective_date": doc.effective_date.isoformat(),
        "visibility": doc.visibility,
        "chunk_id": make_chunk_id(doc.id, chunk.chunk_index),
        "chunk_index": int(chunk.chunk_index),
        "char_start": int(chunk.char_start),
        "char_end": int(chunk.char_end),
        "page": int(page),
        "current_chapter": chapter,
        "chapter_level": int(level),
        # 每个角色一个布尔字段 —— 缺一个，那个角色就永远筛不出「仅勾选他」的文档
        "image_paths": json.dumps(images, ensure_ascii=False),
        "bbox": json.dumps(
            boxes_for_range(page_boxes or [], slices,
                            chunk.char_start, chunk.char_end),
            ensure_ascii=False,
        ),
    }
    for role in ALL_ROLES:
        meta[f"vis_{role}"] = role in roles

    return meta


def role_flags(visible_roles: list[str] | None) -> dict[str, bool]:
    """把 `visible_roles` 展开成 `vis_<角色>` 布尔字段。

    改可见范围后重写 metadata 时用它（§4.3.1.2）。
    """
    roles = set(visible_roles or [])
    return {f"vis_{role}": role in roles for role in ALL_ROLES}
