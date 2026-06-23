"""文档处理核心 — 加载 → 清洗 → 切分 → 入库 → MD5 记录。"""
import os
import re
import hashlib
from pathlib import Path
from datetime import datetime
from app.config.loader import get_config
from app.utils.log_tool import get_logger
from app.rag.md5_manager.md5_store import MD5Store
from app.rag.text_spliter import AsyncTextSplitter
from app.rag.vector_store import VectorStoreService

logger = get_logger(__name__)


def _get_magic_signatures() -> dict[bytes, tuple[str, str]]:
    raw = get_config("magic_signatures", {})
    return {k.encode("utf-8"): (v[0], v[1]) for k, v in raw.items()}


def _clean_text(documents: list) -> list:
    """文本清洗流水线（5 步）。"""
    if not os.getenv("TEXT_CLEAN_ENABLED", "true").lower() == "true":
        return documents

    cleaned = []
    for doc in documents:
        text = doc.page_content

        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r' {2,}', ' ', text)
        text = '\n'.join(line.strip() for line in text.split('\n'))
        text = re.sub(r'第\s*\d+\s*页\s*/\s*共\s*\d+\s*页', '', text)
        text = re.sub(r'^\d{1,4}$', '', text, flags=re.MULTILINE)
        text = re.sub(r'---\s*Page\s*\d+\s*---', '', text)

        if not text.strip():
            continue

        doc.page_content = text.strip()
        cleaned.append(doc)

    return cleaned


def diagnose_failure(file_bytes: bytes, filename: str, loader_errors: list[str] = None) -> dict:
    """诊断兜底：所有 Loader 均失败后，检测魔数给出明确失败原因。"""
    file_size = len(file_bytes)

    if file_size == 0:
        return {
            "status": "failed", "reason": "empty_file",
            "detail": "文件为空", "suggestion": "文件内容为空，请检查后重新上传",
            "filename": filename,
        }

    magic_bytes = file_bytes[:8]
    magic_hex = magic_bytes.hex()
    signatures = _get_magic_signatures()

    for sig_bytes, (reason, detail) in signatures.items():
        if magic_bytes.startswith(sig_bytes):
            suggestion = "文件可能已损坏，请重新导出/保存后上传" if reason == "corrupted" \
                else "不支持该格式，支持的格式：pdf/txt/md/docx/pptx"
            _log_diagnosis(filename, file_size, magic_hex, loader_errors or [], reason, detail)
            return {
                "status": "failed", "reason": reason,
                "detail": detail, "suggestion": suggestion,
                "filename": filename,
            }

    _log_diagnosis(filename, file_size, magic_hex, loader_errors or [], "unknown_format", "无法识别文件类型")
    return {
        "status": "failed", "reason": "unknown_format",
        "detail": "无法识别文件类型",
        "suggestion": "无法识别文件类型，请确认文件格式正确后重新上传",
        "filename": filename,
    }


def _log_diagnosis(filename: str, file_size: int, magic_hex: str,
                   loader_errors: list[str], reason: str, detail: str):
    logger.error(
        f"【诊断兜底】文件: {filename} | 大小: {file_size}B"
        f"{' | 魔数: ' + magic_hex if magic_hex else ''}"
    )
    for err in loader_errors:
        logger.error(f"  Loader 失败: {err}")
    logger.error(f"  诊断: {reason} → {detail}")


class DocumentProcessor:
    """文档处理核心 — 加载 → 清洗 → 切分 → 向量化 → MD5。"""

    def __init__(self):
        self._vector_store = VectorStoreService()
        self._md5_store = MD5Store()
        self._splitter = AsyncTextSplitter()

    async def process(self, file_path: str | Path, user_id: str,
                      original_filename: str = "") -> dict:
        file_path = Path(file_path)
        if not original_filename:
            original_filename = file_path.name

        # 1. MD5
        try:
            with open(file_path, "rb") as f:
                file_bytes = f.read()
            md5_hex = hashlib.md5(file_bytes).hexdigest()
        except Exception as e:
            logger.error(f"【MD5计算】读取文件失败: {e}")
            return {"status": "failed", "reason": str(e), "filename": original_filename}

        # 2. 去重
        if self._md5_store.check_md5_exists(user_id, md5_hex):
            logger.debug(f"【向量数据库】文件 {original_filename} 的 md5 值 {md5_hex} 已存在，跳过")
            return {"status": "duplicate", "md5": md5_hex, "filename": original_filename}

        # 3. 加载
        extension = file_path.suffix.lower().lstrip(".")
        documents = []
        loader_errors = []

        try:
            if extension == "pdf":
                import asyncio
                from app.utils.pdf_multimodal_loader import pdf_multimodal_loader

                blocks = await pdf_multimodal_loader(str(file_path), user_id, md5_hex)
                from langchain_core.documents import Document
                for block in blocks:
                    doc = Document(
                        page_content=block["content"],
                        metadata={
                            "page": block["page_num"],
                            "source": str(file_path),
                            "image_paths": block.get("metadata", {}).get("image_paths", []),
                            "has_images": block.get("metadata", {}).get("has_images", False),
                        },
                    )
                    documents.append(doc)
            else:
                from app.utils.file_handler import load_file
                documents = load_file(file_path, extension)
                if not documents:
                    loader_errors.append(f"{extension.upper()} Loader 返回空列表")
        except Exception as e:
            loader_errors.append(f"{extension.upper()} Loader 异常: {str(e)}")
            logger.error(f"【文档加载】{extension.upper()} 加载失败: {e}")

        if not documents:
            logger.error(f"【向量数据库】文件 {original_filename} 加载内容为空")
            diagnosis = diagnose_failure(file_bytes, original_filename, loader_errors)
            return {"status": "failed", "diagnosis": diagnosis, "filename": original_filename}

        # 4. 清洗
        documents = _clean_text(documents)
        if not documents:
            return {"status": "failed", "reason": "empty_content",
                    "filename": original_filename, "md5": md5_hex}

        # 5. 切分
        documents = await self._splitter.async_split_documents(documents)
        if not documents:
            logger.debug(f"【向量数据库】文件 {original_filename} 切分内容为空，跳过")
            return {"status": "failed", "reason": "empty_content",
                    "filename": original_filename, "md5": md5_hex}

        # 6. 元数据
        for doc in documents:
            chunk_idx = doc.metadata.get("chunk_index", 0)
            doc.metadata["kb_id"] = user_id
            doc.metadata["chunk_index"] = chunk_idx
            doc.metadata["chunk_id"] = f"{user_id}_{md5_hex}_{chunk_idx:04d}"
            doc.metadata["user_id"] = user_id
            doc.metadata["md5"] = md5_hex
            doc.metadata["original_filename"] = original_filename
            doc.metadata["created_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # 7. 向量入库
        try:
            self._vector_store.add_documents(documents)
            logger.info(f"【向量数据库】文件 {original_filename} 入库完成: {len(documents)} 个 chunk")
        except Exception as e:
            logger.error(f"【向量数据库】文件入库失败: {e}")
            return {"status": "failed", "reason": str(e), "filename": original_filename, "md5": md5_hex}

        # 8. MD5 记录（失败则回滚 ChromaDB 写入，保证原子性）
        try:
            self._md5_store.save_md5_hex(user_id, md5_hex, original_filename, str(file_path))
        except Exception as e:
            logger.error(f"【MD5】保存失败，回滚向量数据: {e}")
            try:
                self._vector_store.delete_by_md5(user_id, md5_hex)
            except Exception as re:
                logger.error(f"【MD5】回滚 ChromaDB 也失败: {re}")
            return {"status": "failed", "reason": f"MD5 保存失败: {e}", "filename": original_filename, "md5": md5_hex}

        return {
            "status": "done", "md5": md5_hex,
            "filename": original_filename, "chunks": len(documents),
        }
