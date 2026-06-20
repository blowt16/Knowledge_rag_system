"""多格式文件加载器 — TXT/MD/DOCX/PPTX，含降级链。"""
from pathlib import Path
from app.utils.log_tool import get_logger

logger = get_logger(__name__)

ALLOW_FILE_TYPES = {"txt", "pdf", "md", "pptx", "docx"}


def load_file(file_path: str | Path, extension: str) -> list:
    """根据扩展名选择加载器并加载文档。"""
    file_path = Path(file_path)
    ext = extension.lower().lstrip(".")

    if ext == "txt":
        return txt_loader(file_path)
    elif ext == "md":
        return markdown_loader(file_path)
    elif ext == "docx":
        return docx_loader(file_path)
    elif ext == "pptx":
        return pptx_loader(file_path)
    else:
        raise ValueError(f"不支持的文件格式: .{ext}")


def txt_loader(file_path: Path) -> list:
    """TXT 加载器 — utf-8 → gbk 编码回退。"""
    from langchain_community.document_loaders import TextLoader

    for encoding in ["utf-8", "gbk", "gb2312", "latin-1"]:
        try:
            loader = TextLoader(str(file_path), encoding=encoding)
            docs = loader.load()
            if docs and docs[0].page_content.strip():
                logger.info(f"【文本文件加载】使用编码 {encoding} 成功加载")
                return docs
        except Exception as e:
            logger.debug(f"【文本文件加载】编码 {encoding} 加载失败: {e}")

    logger.error(f"【文本文件加载】所有编码均失败")
    return []


def markdown_loader(file_path: Path) -> list:
    """Markdown 加载器 — UnstructuredMarkdownLoader → TextLoader 兜底。"""
    try:
        from langchain_community.document_loaders import UnstructuredMarkdownLoader
        loader = UnstructuredMarkdownLoader(str(file_path), mode="single")
        docs = loader.load()
        if docs and docs[0].page_content.strip():
            logger.info("【Markdown文件加载】UnstructuredMarkdownLoader 成功加载")
            return docs
    except Exception as e:
        logger.warning(f"【Markdown文件加载】UnstructuredMarkdownLoader 失败: {e}，尝试 TextLoader 兜底")

    try:
        from langchain_community.document_loaders import TextLoader
        loader = TextLoader(str(file_path), encoding="utf-8")
        docs = loader.load()
        if docs and docs[0].page_content.strip():
            logger.info("【Markdown文件加载】TextLoader 兜底成功")
            return docs
    except Exception as e:
        logger.error(f"【Markdown文件加载】TextLoader 兜底也失败: {e}")

    return []


def docx_loader(file_path: Path) -> list:
    """DOCX 加载器 — Docx2txtLoader → python-docx 兜底。"""
    try:
        from langchain_community.document_loaders import Docx2txtLoader
        loader = Docx2txtLoader(str(file_path))
        docs = loader.load()
        if docs and docs[0].page_content.strip():
            logger.info("【WORD文件加载】Docx2txtLoader 成功加载")
            return docs
    except Exception as e:
        logger.warning(f"【WORD文件加载】Docx2txtLoader 失败: {e}，尝试 python-docx 兜底")

    try:
        from docx import Document
        doc = Document(str(file_path))
        text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        if text.strip():
            from langchain_core.documents import Document as LCDoc
            logger.info("【WORD文件加载】python-docx 兜底成功")
            return [LCDoc(page_content=text, metadata={"source": str(file_path)})]
    except Exception as e:
        logger.error(f"【WORD文件加载】python-docx 兜底也失败: {e}")

    return []


def pptx_loader(file_path: Path) -> list:
    """PPTX 加载器 — UnstructuredPowerPointLoader → python-pptx 兜底。"""
    try:
        from langchain_community.document_loaders import UnstructuredPowerPointLoader
        loader = UnstructuredPowerPointLoader(str(file_path), mode="single")
        docs = loader.load()
        if docs and docs[0].page_content.strip():
            logger.info("【PPT文件加载】UnstructuredPowerPointLoader 成功加载")
            return docs
    except Exception as e:
        logger.warning(f"【PPT文件加载】UnstructuredPowerPointLoader 失败: {e}，尝试 python-pptx 兜底")

    try:
        from pptx import Presentation
        prs = Presentation(str(file_path))
        texts = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        t = paragraph.text.strip()
                        if t:
                            texts.append(t)
        text = "\n".join(texts)
        if text.strip():
            from langchain_core.documents import Document as LCDoc
            logger.info("【PPT文件加载】python-pptx 兜底成功")
            return [LCDoc(page_content=text, metadata={"source": str(file_path)})]
    except Exception as e:
        logger.error(f"【PPT文件加载】python-pptx 兜底也失败: {e}")

    return []
