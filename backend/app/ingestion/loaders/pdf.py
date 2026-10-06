"""PDF 加载器 —— 两路分支（§3.4.2 / §E.4.2 / §E.8）。

    每页 → PyMuPDF 一次遍历（同时完成：文字提取 + 图片提取 + 类型判定）
            ├─ 文字层可信 → 直接出 Document（带页码 + 正则章节）
            ├─ 无文字 / 不可信 → 交 MinerU（**按页限定范围**，见 E.8.6）
            └─ 图片提取 → 落盘 + 写 metadata 的 image_paths

⚠️ 一次遍历同时做三件事（E.8.3 ③）：原链路同一个 PDF 被 fitz.open 打开 3–4 次
   （类型判定 → 取图 → 各分支），每次都重建 xref 与页树。

⚠️ 原「三路分支」已废弃：「图文混排 → VL 流水线」整条删除（E.4.5）——
   判据是「文字**可不可信**」，不是「有没有图」。首页一个公章不足以把整份公文
   拖进 OCR（本语料 6/10 份因此中招）。

⚠️ 竖排检测必须在**清洗之前**（E.3.3）：清洗会先删掉「成文日期」这类单字符行，
   反转再也救不回来。

⚠️ MinerU 限页码后 page_idx 是**相对重数**，入库前必须重映射（E.8.3 ③ / 附录 F.2）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF

from app.core.config import cfg
from app.ingestion.loaders.base import ChapterMark, LoadResult, PageText

__all__ = [
    "ChapterMark", "LoadResult", "PageText",
    "load_pdf", "extract_chapter_marks", "bbox_tag",
    "single_char_line_ratio", "reverse_line_order", "garbled_ratio", "is_garbled_char",
]

# ---- 乱码判据（§E.8.1，照搬 RAGFlow / MinerU）------------------------

_PUA_RANGE = (0xE000, 0xF8FF)


def is_garbled_char(ch: str) -> bool:
    """乱码字符：PUA、U+FFFD、控制字符、Unicode Cn/Cs 类。"""
    code = ord(ch)
    if _PUA_RANGE[0] <= code <= _PUA_RANGE[1]:
        return True
    if code == 0xFFFD:
        return True
    if ch in "\n\r\t":
        return False
    if unicodedata.category(ch) in ("Cn", "Cs", "Cc"):
        return True
    return False


def garbled_ratio(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    return sum(1 for c in chars if is_garbled_char(c)) / len(chars)


# ---- 竖排检测（§E.3.3）-----------------------------------------------

def single_char_line_ratio(text: str) -> float:
    """单字符行占非空行的比例。

    「每字一行」就是最好的检测信号 —— 实测 10 份首页区分度非常干净：
    前 5 份 0%，06 是 50%，10 是 56%，07/08/09 是 100%。

    ⚠️ 判据必须写成 >= 0.5，不能写 > 0.5 —— 文档 06 恰好卡在 50%，
       用严格大于会漏掉它。
    """
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return 0.0
    return sum(1 for ln in lines if len(ln.strip()) == 1) / len(lines)


def reverse_line_order(text: str) -> str:
    """竖排恢复：**反转行序并合并各行**（§E.3.3）。

    ⚠️ 是「反转行序」，**不是**「反转全部字符」：
       文档 06 混有多字行，反转字符会得到乱码（`：门部各、位单各请…`），
       反转行序对整页倒序与部分倒序两类都正确。

    ⚠️⚠️ **必须同时合并各行（去掉空白行、逐行 strip 后无分隔拼接）** ——
       这一点文档正文只写了「反转行序」，字面照做会**静默毁掉成文日期**。
       2026-10-05 在真实语料上实测：

       | 做法 | 文档 07 首页结果 |
       |---|---|
       | 反转行序 + **保留换行** | 仍是每字一行 → `2019`/`2021`/`2025` 依旧是独立行 → 被清洗规则 `^\\d{1,4}$` 当页码删掉，**年份丢失** |
       | 反转行序 + **合并** | `—1—各单位、各部门：现将《…（2025年修订）》印发给你们，请遵照执行。桂林电子科技大学2025年7月10日（此件公开发布）` —— 与 §E.3.3 给出的示例输出**逐字吻合** |

       两份实测数据都指向「合并」：① 文档 07 的期望输出是一整行；② 文档自己说
       「若先清洗再反转，成文日期会被毁掉」—— 该论断只有在合并的前提下才成立
       （不合并的话，日期行无论反转前后都是独立数字行，都会被删）。

    ⚠️ 动作是「修复」而非「过滤」：丢弃会白白损失首页的**文种标题、发文机关、
       成文日期**（文号在红头图上，文字层本来就没有，两种做法都拿不到）。
    """
    lines = [ln.strip() for ln in text.split("\n")]
    return "".join(ln for ln in reversed(lines) if ln)


# ---- 章节正则（§E.4.2）----------------------------------------------

# ⚠️ 必须用宽松口径（`第X章` 出现在行内即可），不要要求它独占一行：
#    严格口径命中 31 个，宽松口径命中 58 个，差的 27 个全在 01/05/06/07 四份里
#    —— 它们的章标题与后续文字排在同一行。
_CHAPTER_RE = re.compile(r"第\s*([一二三四五六七八九十百零〇\d]+)\s*章")
_ARTICLE_RE = re.compile(r"第\s*([一二三四五六七八九十百零〇\d]+)\s*条")
# 顶层条号的另一种形态：「一、」**在行首**（允许前导空白）。
# ⚠️ 只作回退用 —— 见 `extract_chapter_marks` 的门槛说明。
_SECTION_RE = re.compile(r"^[ \t]*[一二三四五六七八九十]+\s*、", re.M)
_CN_NUM = {c: i for i, c in enumerate("零一二三四五六七八九", start=0)}


def _cn_to_int(text: str) -> int:
    """把「十二」「二十」这类中文数字转成 int；纯数字直接转。"""
    text = text.strip()
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    total, section = 0, 0
    for ch in text:
        if ch == "十":
            section = (section or 1) * 10
        elif ch in _CN_NUM:
            section += _CN_NUM[ch]
        else:
            return 0
    total += section
    return total


def extract_chapter_marks(text: str) -> list[ChapterMark]:
    """正则抽取章节标记 —— 公文结构极规整，不需要任何模型。

    ⚠️ **回退规则**（M3 的 K-2）：有些公文用「一、二、」作**顶层条号**，
       全文没有「第X章」—— `10_参军入伍…桂电2021-2号` 就是，实测
       整份 5 条 chunk 一条章节都抽不出来、引用只能落到「只跳页」。

       ⚠️ 回退**必须有门槛**：只在「一个章标记都抽不到」时才启用。
          「一、」在别的公文里是**条内的枚举**（如「一、申请条件」），
          无条件认它会把枚举误判成章节、把 9 份公文的结构搞乱。
          实测：那 9 份行首「一、」都是 0 次，但那是这份语料恰好如此，
          不能指望以后传的也这样。
    """
    marks: list[ChapterMark] = []
    for m in _CHAPTER_RE.finditer(text):
        line_end = text.find("\n", m.start())
        title = text[m.start():line_end if line_end != -1 else len(text)].strip()
        marks.append(ChapterMark(chapter=title[:60], level=1, char_offset=m.start()))

    if not marks:
        for m in _SECTION_RE.finditer(text):
            line_end = text.find("\n", m.start())
            title = text[m.start():line_end if line_end != -1 else len(text)].strip()
            marks.append(ChapterMark(chapter=title[:60], level=1, char_offset=m.start()))

    for m in _ARTICLE_RE.finditer(text):
        line_end = text.find("\n", m.start())
        title = text[m.start():line_end if line_end != -1 else len(text)].strip()
        marks.append(ChapterMark(chapter=title[:60], level=2, char_offset=m.start()))
    marks.sort(key=lambda x: x.char_offset)
    return marks


# ---- 结果结构见 loaders/base.py ---------------------------------------

def bbox_tag(page_no: int, rect: "fitz.Rect") -> str:
    """RAGFlow 的 bbox 格式（§E.8.4）：`@@{页号}\\t{x0}\\t{x1}\\t{top}\\t{bottom}##`

    ⚠️ 该字符串存为 **Chroma metadata 的独立字段 `bbox`，不进 chunk 正文**。
       进正文会同时污染三处：向量（坐标串被一起嵌入）、BM25（坐标串参与打分）、
       `Citation.snippet`（给用户看的摘录混进 `@@3\\t120.5...##` 就是脏数据）。
    """
    return f"@@{page_no}\t{rect.x0:.1f}\t{rect.x1:.1f}\t{rect.y0:.1f}\t{rect.y1:.1f}##"


# ---- 主流程 -----------------------------------------------------------

def image_coverage(page: "fitz.Page") -> float:
    """该页被图片覆盖的面积比。**页级**判据（§E.8.1）。"""
    page_area = abs(page.rect.width * page.rect.height)
    if page_area <= 0:
        return 0.0
    covered = 0.0
    try:
        images = page.get_images(full=True)
    except Exception:  # noqa: BLE001
        return 0.0
    for img in images:
        try:
            for rect in page.get_image_rects(img[0]):
                covered += abs(rect.get_area())
        except Exception:  # noqa: BLE001
            continue
    return min(covered / page_area, 1.0)


# ---- 子集字体把汉字映射成 ASCII（§E.8.1，页级）-----------------------
#
# 现场：老式中文字体把汉字编码到 ASCII 码位，**抽出来是标点汤** ——
# 它既不是 PUA 也不是 U+FFFD，`garbled_ratio` 一点都抓不到，
# 于是以「看着正常」的正文进入索引（检索与生成的输入全被污染）。
#
# 判据（照搬 RAGFlow `pdf_parser.py:317-366`）：子集占比 ≥0.3 **且**
# CJK <0.05 **且** ASCII 标点 >0.4 —— 三条**同时**成立才算。
# 单看「子集占比」会大面积误判：现代 PDF 默认就嵌子集字体，
# 正常中文公文同样满足，真正区分开的是后两条。
_SUBSET_PREFIX_LEN = 6


def _subset_font_names(page: "fitz.Page") -> set[str]:
    """该页的子集字体名（去掉 `XXXXXX+` 前缀，与 span['font'] 的写法对齐）。

    ⚠️ 前缀只出现在 `get_fonts()` 的 basefont 上；`get_text("rawdict")` 的
       `span['font']` **不带前缀**（实测 `DJHPWB+SimHei Regular` vs
       `SimHei Regular`）—— 不去前缀就一条都对不上，判据永远为假。
    """
    names: set[str] = set()
    try:
        fonts = page.get_fonts(full=True)
    except Exception:  # noqa: BLE001
        return names
    for font in fonts:
        base = (font[3] or "") if len(font) > 3 else ""
        head, sep, tail = base.partition("+")
        if sep and len(head) == _SUBSET_PREFIX_LEN and tail:
            names.add(tail)
    return names


def _subset_font_ratio(page: "fitz.Page", subset: set[str]) -> float:
    """用子集字体排出来的字符占比（按字符数加权，不是按 span 数）。"""
    total = hit = 0
    try:
        raw = page.get_text("rawdict")
    except Exception:  # noqa: BLE001
        return 0.0
    for block in raw.get("blocks", []):
        if block.get("type") != 0:      # 只数文字块
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                n = len(span.get("chars", []))
                total += n
                if span.get("font") in subset:
                    hit += n
    return hit / total if total else 0.0


def _cjk_ratio(chars: list[str]) -> float:
    cjk = sum(1 for c in chars if "一" <= c <= "鿿" or "㐀" <= c <= "䶿")
    return cjk / len(chars) if chars else 0.0


def _ascii_punct_ratio(chars: list[str]) -> float:
    punct = sum(1 for c in chars if c.isascii() and not c.isalnum())
    return punct / len(chars) if chars else 0.0


def subset_font_suspect(page: "fitz.Page", text: str) -> bool:
    """页级：这一页的文字层是不是「子集字体把汉字映射成 ASCII」（§E.8.1）。"""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return False

    # ⚠️ 先算便宜的两条、不满足就直接返回：`get_text("rawdict")` 比
    #    `get_text()` 贵得多，而正常中文公文（CJK 占比高）**根本到不了**
    #    字体分析那一步。这是纯性能短路，不改判据语义 —— 三条是「与」。
    if _cjk_ratio(chars) >= float(cfg("ingestion.quality_gate.subset_cjk_ratio", 0.05)):
        return False
    if _ascii_punct_ratio(chars) <= float(cfg("ingestion.quality_gate.subset_ascii_punct_ratio", 0.4)):
        return False

    subset = _subset_font_names(page)
    if not subset:
        return False
    return _subset_font_ratio(page, subset) >= float(
        cfg("ingestion.quality_gate.subset_font_ratio", 0.3))


def _page_is_usable(page: "fitz.Page", text: str) -> bool:
    """**页级**判据：这页的文字层可不可信（§E.8.1）。

    ⚠️⚠️ 页级与整份级判据**不能混用** —— 这是文档明确警告过的一处：

    | 检测项 | 适用范围 |
    |---|---|
    | 页级乱码率、框级乱码率、子集字体、乱码字符 | **页 / 框级** —— 不合格的**那一页**单独走 OCR |
    | 平均每页有效字符、图片覆盖率 | **整份文档级** —— 用于**初判这份文件是不是扫描件** |

    2026-10-05 实测踩过这个坑：曾把整份级的 `min_chars_per_page: 50` 当页级阈值用，
    结果 5 份公文各多送了一页给 MinerU —— 那些是**文末版记页**
    （「桂林电子科技大学校长办公室 / 2025 年 7 月 26 日印发」，34–44 字），
    内容清晰可读，只是短。**白烧云 API 调用，且完全不影响不了正确性、很难发现。**

    页级只用两条：① 乱码率 ② 图片覆盖率。**不设字符数下限** ——
    真正无文字层的扫描页会因 `text` 为空而被拦下。
    """
    stripped = text.strip()
    if not stripped:
        return False

    # 页级乱码率：≥30% → 该页走 OCR
    garbled_threshold = float(cfg("ingestion.quality_gate.page_garbled_ratio", 0.3))
    if garbled_ratio(stripped) >= garbled_threshold:
        return False

    # 子集字体把汉字映射成 ASCII（页级，§E.8.1）
    if subset_font_suspect(page, stripped):
        return False

    # 页级图片覆盖率：≥0.8 → 该页判为扫描页
    coverage_threshold = float(cfg("ingestion.quality_gate.image_coverage_ratio", 0.8))
    if image_coverage(page) >= coverage_threshold:
        return False

    return True


def document_looks_scanned(doc: "fitz.Document", texts: list[str]) -> bool:
    """**整份文档级**初判：这份文件是不是扫描件（§E.8.1）。

    只在整份层面用，**不要拿它逐页判** ——
    一份 100 页公文里只有 2 页扫描页时，整份平均远超 50，
    拿这个当页级阈值会让那 2 页永远进不了 OCR、被静默当正文处理。
    """
    if not texts:
        return True
    threshold = int(cfg("ingestion.quality_gate.min_chars_per_page", 50))
    average = sum(len(t.strip()) for t in texts) / len(texts)
    return average < threshold


def _extract_images(doc: "fitz.Document", page: "fitz.Page", page_no: int,
                    out_dir: Path) -> list[str]:
    """图片提取与落盘 —— **保留**（只砍 VL 描述，见 E.4.5）。

    引用面板的缩略图依赖它。

    ⚠️ B.3.2：不要为拿宽高调用 `doc.extract_image(xref)` ——
       `page.get_images(full=True)` 的元组**本来就含宽高**（索引 2/3）。
    """
    saved: list[str] = []
    try:
        images = page.get_images(full=True)
    except Exception:  # noqa: BLE001 — 取图失败不该让整份文档失败
        return saved

    for index, img in enumerate(images):
        xref = img[0]
        width, height = img[2], img[3]          # ← 直接从元组取，不额外提取
        if width < 30 or height < 30:            # 过滤装饰性小图
            continue
        try:
            info = doc.extract_image(xref)
        except Exception:  # noqa: BLE001
            continue
        data = info.get("image")
        ext = info.get("ext", "png")
        if not data:
            continue
        name = f"p{page_no}_{index}.{ext}"
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            (out_dir / name).write_bytes(data)
            saved.append(name)
        except OSError:
            continue
    return saved


def _text_blocks(page: "fitz.Page") -> tuple[str, list[dict]]:
    """取文字 + 逐块 bbox。"""
    blocks = page.get_text("blocks")
    parts: list[str] = []
    boxes: list[dict] = []
    for block in blocks:
        x0, y0, x1, y1, text = block[0], block[1], block[2], block[3], block[4]
        if not text or not text.strip():
            continue
        parts.append(text)
        boxes.append({"x0": round(x0, 1), "x1": round(x1, 1),
                      "top": round(y0, 1), "bottom": round(y1, 1)})
    return "\n".join(parts), boxes


def load_pdf(path: Path, *, image_dir: Path | None = None) -> LoadResult:
    """加载 PDF。文字层可信的页本地提取，不可信的页交给 MinerU（按页限定）。

    ⚠️ 一次 fitz.open，同时完成类型判定 + 文字提取 + 图片提取。
    """
    result = LoadResult()
    needs_ocr: list[int] = []

    doc = fitz.open(path)
    try:
        total = doc.page_count
        extracted: list[tuple[int, "fitz.Page", str, list[dict]]] = []
        for index in range(total):
            page = doc.load_page(index)
            text, boxes = _text_blocks(page)
            extracted.append((index + 1, page, text, boxes))   # 1-based 绝对页码

        # 整份级初判（§E.8.1）：整份像扫描件时，所有页都走 OCR。
        # 注意它是**整份**判据，不能拿它逐页判。
        whole_scanned = document_looks_scanned(doc, [t for _, _, t, _ in extracted])

        for page_no, page, text, boxes in extracted:
            if (not whole_scanned) and _page_is_usable(page, text):
                # 竖排检测必须在清洗之前（E.3.3）
                ratio = single_char_line_ratio(text)
                if ratio >= float(cfg("ingestion.vertical_text_single_char_ratio", 0.5)):
                    text = reverse_line_order(text)

                result.pages.append(PageText(
                    page=page_no,
                    text=text,
                    bbox=[{"page": page_no, **b} for b in boxes],
                    image_paths=_extract_images(doc, page, page_no, image_dir)
                                  if image_dir else [],
                ))
            else:
                # 不合格的页单独走 OCR —— 不是整份文档（§E.8.6）
                needs_ocr.append(page_no)
    finally:
        doc.close()

    if needs_ocr:
        ocr_pages = _ocr_pages(path, needs_ocr, image_dir)
        result.pages.extend(ocr_pages)
        result.ocr_pages = sorted(needs_ocr)
        result.source = "mixed" if result.pages and len(result.pages) > len(ocr_pages) else "mineru"

    result.pages.sort(key=lambda p: p.page)

    # 缺失页清单（B.1.2）—— 空页不能只是打个 warning 就跳过
    present = {p.page for p in result.pages if p.text.strip()}
    doc_pages = total
    result.missing_pages = [n for n in range(1, doc_pages + 1) if n not in present]
    return result


def _ocr_pages(pdf_path: Path, pages: list[int], image_dir: Path | None) -> list[PageText]:
    """把指定的页交给 MinerU —— **按页限定范围**（§E.8.6）。

    ⚠️ 不把整份 PDF 送出去：100 页公文只 2 页扫描件时，
       整份送 = 做了 50 倍的功，云 API 成本与等待时间都是。

    ⚠️ MinerU 的 `page_idx` 是 **0-based、相对本次请求的子集**，
       不是绝对页码。入库前必须重映射：
           绝对页码(1-based) = 请求起始页 + page_idx
       否则**扫件的引用回跳会全部跳错页**（扫件是三条路径里最依赖页码的：
       PDF 有 bbox、docx 走文本匹配，只有扫件两条都弱）。
    """
    from app.ingestion.mineru_client import extract_pages

    # 把连续的页码压成区间，减少请求次数
    spans = _to_spans(pages)
    out: list[PageText] = []
    for start, end in spans:
        page_range = f"{start}-{end}" if end > start else str(start)
        blocks = extract_pages(pdf_path, page_range)
        # blocks 里每项带相对 page_idx
        grouped: dict[int, list[str]] = {}
        for block in blocks:
            absolute = start + int(block["page_idx"])   # ★ 重映射，别丢
            grouped.setdefault(absolute, []).append(block.get("text", ""))
        for absolute, texts in grouped.items():
            out.append(PageText(page=absolute, text="\n".join(texts)))
    return out


def _to_spans(pages: list[int]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for p in sorted(pages):
        if spans and p == spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], p)
        else:
            spans.append((p, p))
    return spans
