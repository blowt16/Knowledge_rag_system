"""加载器的共用结果结构。

各格式的 `page` 语义（§3.4.2，原方案未定案，此处钉死）：

| 格式 | page | 说明 |
|---|---|---|
| PDF  | 真实页码，1 起 | 文字层可信的页从 PyMuPDF 取；扫描页从 MinerU 取（重映射为绝对页） |
| DOCX | **恒为 1** | docx 的分页是**渲染产物**、不是文档固有属性；定位走文本匹配（boxes 为空 → 从 L2 起步） |
| PPTX | **幻灯片序号，从 1 起** | **必须输出** —— 不给序号，pptx 的引用回跳与分块预览都定位不到「第几页」 |
| MD / TXT | **恒为 1** | 与 docx 同理 |

⚠️ PPTX 的 page 直接复用现有字段，不新增 `slide_index`：语义一致（「第几页」），
   复用能避免下游（jump_target / 分块预览 / 引用卡片）出现两套页码概念。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChapterMark:
    chapter: str      # 如「第三章 奖励与处分」
    level: int        # 1 = 章，2 = 条/节，3 = （一）级
    char_offset: int  # 在规范化文本中的起始偏移


@dataclass
class PageText:
    page: int                     # 1-based
    text: str                     # 已做竖排恢复，**未清洗**
    bbox: list[dict] = field(default_factory=list)
    image_paths: list[str] = field(default_factory=list)


@dataclass
class LoadResult:
    pages: list[PageText] = field(default_factory=list)
    missing_pages: list[int] = field(default_factory=list)
    ocr_pages: list[int] = field(default_factory=list)
    # 结构化来源的章节标记（docx 标题层级 / md 目录）；PDF 走正则抽取
    heading_marks: list[ChapterMark] = field(default_factory=list)
    source: str = "local"         # local | mineru | mixed
