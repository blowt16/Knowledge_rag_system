"""章节抽取（§3.4.2 / E.3.2）—— M3 的 K-2 跟进。

⚠️ **实测背景**（不是推断）：语料 148 条 chunk 里 23 条 `current_chapter` 为空。
   查下去是**两类**：

   - **18 条**：9 份公文各自的 chunk#0/#1，即首页红头/文号/标题区 ——
     它们在「第一条」**之前**，本来就没有章节。**不是缺陷**。
   - **5 条**：`10_参军入伍…桂电2021-2号` 这一份**整份**没有章节 ——
     它用「一、二、」作顶层条号，全文没有「第X章」/「第X条」。

   所以修法是**加一条回退**，且只在「一个章标记都抽不到」时才启用 ——
   「一、」在别的公文里是**条内的枚举**，无条件认它会把枚举误判成章节。
   实测支撑：另外 9 份公文行首「一、」**都是 0 次**；但那是这份语料恰好如此，
   不能指望以后传的也这样 —— 所以回退必须有门槛。
"""

from __future__ import annotations

from app.ingestion.loaders.pdf import extract_chapter_marks

NORMAL_DOC = (
    "第一条 为规范管理，制定本办法。\n"
    "一、申请条件\n"
    "1. 在校学生；\n"
    "二、办理流程\n"
    "第二章 学籍管理\n"
    "第三条 学生应当……\n"
    "一、转专业\n"
)

SECTION_ONLY_DOC = (
    "桂电教[2021]2号\n\n"
    "一、2021 年以后报考的应征入伍学生，给予一次性奖励。\n"
    "二、应征入伍在校生退役复学后，减免住宿费。\n"
    "三、在读期间成绩认定按《…办法》执行。\n"
)


def test_normal_document_unaffected_by_fallback():
    """★ 回归锁：有「第X章」的文档，**不该**因为新回退而多出「一、」章节。"""
    marks = extract_chapter_marks(NORMAL_DOC)

    chapters = [m.chapter for m in marks if m.level == 1]
    assert chapters == ["第二章 学籍管理"], (
        f"回退不该在已有章标记的文档上生效：{chapters}"
    )
    assert all(not m.chapter.startswith("一、") for m in marks), \
        "条内的「一、」被误判成章节了"


def test_fallback_used_when_no_chapter_marks_at_all():
    """★ 整份没有「第X章」时，用「一、二、」作顶层条号（doc 10 的形态）。"""
    marks = extract_chapter_marks(SECTION_ONLY_DOC)

    chapters = [m.chapter for m in marks if m.level == 1]
    assert len(chapters) == 3, f"回退没生效：{chapters}"
    assert chapters[0].startswith("一、")


def test_no_marks_when_neither_form_exists():
    """两种形态都没有 → 老老实实返回空，不要硬凑。"""
    assert extract_chapter_marks("这是一段没有任何条号的正文。\n再来一行。") == []


def test_fallback_marks_are_ordered_by_offset():
    text = "一、甲\n正文\n二、乙\n正文\n三、丙"
    offsets = [m.char_offset for m in extract_chapter_marks(text) if m.level == 1]
    assert offsets == sorted(offsets) and len(offsets) == 3


def test_fallback_ignores_inline_section_numbers():
    """行**中间**的「一、」不算标题 —— 只在行首（允许前导空白）才算。"""
    text = "本条所称一、二类学生，按下列规定办理。\n正文。"
    assert extract_chapter_marks(text) == []
