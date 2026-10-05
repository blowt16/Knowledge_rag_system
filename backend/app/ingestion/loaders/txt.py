"""TXT 加载器（§3.4.2）。

编码回退链 UTF-8 / GBK / GB18030 依次尝试。

⚠️ 旧项目把这段逻辑写了两遍（`file_handler.py:37-48` txt、`:57-71` md），
   且 md 的降级路径又读了第三次（附录 B.2.4）—— 收敛到
   `file_type.detect_encoding()` 一处。

⚠️ `page` **恒为 1**。
"""

from __future__ import annotations

from pathlib import Path

from app.ingestion.file_type import detect_encoding
from app.ingestion.loaders.base import LoadResult, PageText


def load_txt(path: Path, **_kwargs) -> LoadResult:
    encoding = detect_encoding(path)
    text = path.read_text(encoding=encoding, errors="replace")

    result = LoadResult()
    if text.strip():
        result.pages.append(PageText(page=1, text=text))
    return result
