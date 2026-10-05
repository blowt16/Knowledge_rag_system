"""格式嗅探 —— 一处定义、三处复用（§E.4.3）。

调用点：
  ① 上传入口（早拦，给明确提示）
  ② 压缩包内逐文件（旧项目 zip_handler 自己复制了一份扩展名白名单）
  ③ 解析失败后的兜底诊断（旧项目 processor.py 的 magic_signatures，三分之二失配）

⚠️ 别踩历史坑：旧 chroma.yaml 里留着注释
   `# "PK\\x03\\x04" removed: ... this signature caused misdiagnosis`
   裸判 PK 分不出「普通 zip」和「docx」，当年就是这么误判的 ——
   **必须配合容器内部结构**。

判据（零新增依赖，纯 Python）：
    pdf       头部 4 字节 `%PDF`
    docx/pptx 头部 `PK\\x03\\x04`，再往 zip 里看一层：
              有 `[Content_Types].xml`，且 `word/` → docx、`ppt/` → pptx
    txt/md    无可信签名，只能验「能否按 text_encodings 解码」

定位：这层是**给人看的**（快速 + 提示语说人话），**不是安全边界**。
      真正的把关是解析器本身（PyMuPDF / python-docx 遇到假文件会直接拒绝）。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from app.core.config import cfg

PDF_MAGIC = b"%PDF"
ZIP_MAGIC = b"PK\x03\x04"

# OOXML 容器内的标志性路径
_CONTENT_TYPES = "[Content_Types].xml"
_WORD_MARKER = b"word/"
_PPT_MARKER = b"ppt/"

# 头部扫描窗口：本地文件头在最前面，[Content_Types].xml 通常是第一个条目
_HEAD_SCAN = 8192


def sniff_format(head: bytes) -> str | None:
    """从文件头部字节判断格式。无法判断时返回 None（可能是纯文本）。

    只依赖头部，适用于上传流的早拦。
    """
    if not head:
        return None

    if head[:4] == PDF_MAGIC:
        return "pdf"

    if head[:4] == ZIP_MAGIC:
        window = head[:_HEAD_SCAN]
        if _CONTENT_TYPES.encode() not in window:
            # 是 zip，但不是 OOXML —— 可能是普通压缩包
            return None
        if _WORD_MARKER in window:
            return "docx"
        if _PPT_MARKER in window:
            return "pptx"
        # OOXML 但既非 word 也非 ppt（如 xlsx）—— 本项目不支持
        return None

    return None


def sniff_format_from_path(path: Path) -> str | None:
    """从磁盘文件判断格式。OOXML 走真正的 zip 目录读取，比头部扫描可靠。"""
    with open(path, "rb") as f:
        head = f.read(_HEAD_SCAN)

    if head[:4] == PDF_MAGIC:
        return "pdf"

    if head[:4] == ZIP_MAGIC:
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
        except (zipfile.BadZipFile, OSError):
            return None
        if _CONTENT_TYPES not in names:
            return None  # 普通 zip，不是 OOXML
        if any(n.startswith("word/") for n in names):
            return "docx"
        if any(n.startswith("ppt/") for n in names):
            return "pptx"
        return None

    return None


def is_text_decodable(head: bytes) -> bool:
    """txt / md 没有可信签名，只能验「能否按配置的编码解出来」。

    用头部字节试解码即可 —— 二进制文件几乎必然在头几百字节里就失败。
    """
    if not head:
        return False
    if b"\x00" in head:  # NUL 字节是二进制的强信号
        return False
    for enc in cfg("ingestion.text_encodings", ["utf-8", "gbk", "gb18030"]):
        try:
            head.decode(enc)
            return True
        except (UnicodeDecodeError, LookupError):
            continue
    return False


def detect_encoding(path: Path) -> str:
    """按 txt/md 的编码回退链确定编码（UTF-8 / GBK / GB18030 依次尝试）。

    旧项目 file_handler.py 把这段逻辑写了两遍（:37-48 txt、:57-71 md），
    且 md 的降级路径又读了第三次 —— 收敛到这里（附录 B.2.4）。
    """
    raw = path.read_bytes()
    for enc in cfg("ingestion.text_encodings", ["utf-8", "gbk", "gb18030"]):
        try:
            raw.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "utf-8"  # 全部失败时交给 errors="replace"


def extension_of(filename: str) -> str:
    return Path(filename).suffix.lower().lstrip(".")


def is_supported(filename: str, head: bytes | None = None,
                 path: Path | None = None) -> tuple[bool, str]:
    """统一入口：扩展名 + 内容嗅探双重校验。

    返回 (是否支持, 给用户看的错误提示)。

    ⚠️ 上传入口、压缩包内逐文件、诊断兜底三处都调这一个函数 ——
       避免「三处各有一份白名单」的旧问题（附录 B.2.4 / E.4.3）。
    """
    allowed = cfg("ingestion.allowed_extensions", ["pdf", "docx", "pptx", "md", "txt"])
    ext = extension_of(filename)

    if ext not in allowed:
        return False, f"不支持的文件格式：.{ext}（支持 {'/'.join(allowed)}）"

    if path is not None:
        fmt = sniff_format_from_path(path)
        with open(path, "rb") as f:
            head = f.read(_HEAD_SCAN)
    elif head is not None:
        fmt = sniff_format(head)
    else:
        return True, ""

    if ext in ("txt", "md"):
        if fmt is None and is_text_decodable(head or b""):
            return True, ""
        if fmt is None:
            return False, f"文件 {filename} 无法按文本编码解析（支持 UTF-8 / GBK / GB18030）"
        return True, ""

    if fmt is None:
        return False, _unsupported_hint(filename, head or b"")

    if fmt != ext:
        return False, f"文件内容与扩展名不符：扩展名是 .{ext}，内容实际是 {fmt}"

    return True, ""


def _unsupported_hint(filename: str, head: bytes) -> str:
    """内容嗅探失败时，尽量给出「说人话」的提示。

    旧项目的签名表被 UTF-8 编码坏了（E.3.1），把这几种都退化成了
    「无法识别文件类型」—— 用户看不懂。这里按真实文件头给具体名字。
    """
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return f"文件 {filename} 实际是 PNG 图片，不是可解析的文档"
    if head[:3] == b"\xff\xd8\xff":
        return f"文件 {filename} 实际是 JPEG 图片，不是可解析的文档"
    if head[:4] == b"\xd0\xcf\x11\xe0":
        return f"文件 {filename} 实际是旧版 Office 文档（.doc/.ppt/.xls），请另存为新格式后上传"
    if head[:5] == b"GIF8":
        return f"文件 {filename} 实际是 GIF 图片，不是可解析的文档"
    if head[:4] == ZIP_MAGIC:
        return f"文件 {filename} 是个压缩包，但不是受支持的 docx/pptx"
    return f"无法识别文件 {filename} 的类型"


def head_bytes(path: Path, size: int = _HEAD_SCAN) -> bytes:
    with open(path, "rb") as f:
        return f.read(size)
