"""知识库业务逻辑层。"""
import os
import hashlib
import tempfile
from pathlib import Path
from app.config.loader import get_config
from app.rag.document_handler.processor import DocumentProcessor
from app.rag.vector_store import VectorStoreService
from app.rag.md5_manager.md5_store import MD5Store
from app.rag.retrievers.hybrid_retriever import HybridRetriever
from app.utils.log_tool import get_logger
from app.utils.path_tool import get_data_path

logger = get_logger(__name__)


def _get_allow_extensions() -> set[str]:
    return set(get_config("allow_knowledge_file_types", ["txt", "pdf", "md", "pptx", "docx"]))


def _get_mime_types() -> dict[str, str]:
    return get_config("allowed_mime_types", {})


def _get_max_file_size() -> int:
    return int(os.getenv("MAX_FILE_SIZE", "31457280"))


class KnowledgeService:
    """知识库业务逻辑层。"""

    def __init__(self):
        self._processor = DocumentProcessor()
        self._vector_store = VectorStoreService()
        self._md5_store = MD5Store()

    async def upload_single(self, file_bytes: bytes, filename: str, user_id: str) -> dict:
        suffix = Path(filename).suffix
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        try:
            result = await self._processor.process(
                file_path=tmp_path, user_id=user_id, original_filename=filename)
            if result.get("status") == "done":
                HybridRetriever.invalidate_cache(user_id)
            return result
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def validate_file(self, file_bytes: bytes, filename: str) -> dict:
        max_size = _get_max_file_size()
        if len(file_bytes) > max_size:
            return {"valid": False, "error": f"文件大小超过限制（最大 {max_size // 1048576}MB）"}

        ext = Path(filename).suffix.lower().lstrip(".")
        allowed_exts = _get_allow_extensions()
        if ext not in allowed_exts:
            return {"valid": False, "error": f"不支持的文件格式: .{ext}"}

        try:
            import magic
            buf_size = get_config("mime_detect_buffer_size", 2048)
            mime_type = magic.from_buffer(file_bytes[:buf_size], mime=True)
            mime_map = _get_mime_types()
            if mime_type in mime_map:
                return {"valid": True}
        except ImportError:
            pass

        if ext in allowed_exts:
            return {"valid": True}

        return {"valid": False, "error": "文件类型校验失败"}

    def delete_by_md5(self, user_id: str, md5: str):
        self._vector_store.delete_by_md5(user_id, md5)
        self._md5_store.delete_single_md5(user_id, md5)
        self._delete_image_directory(user_id, md5)
        HybridRetriever.invalidate_cache(user_id)

    def clear_user(self, user_id: str):
        self._vector_store.delete_by_user(user_id)
        self._md5_store.clear_user(user_id)
        self._delete_user_images(user_id)
        HybridRetriever.invalidate_cache(user_id)

    def get_documents(self, user_id: str) -> list[dict]:
        return self._md5_store.get_user_documents_info(user_id)

    def _delete_image_directory(self, user_id: str, md5: str):
        import shutil
        shutil.rmtree(get_data_path(f"extracted_images/{user_id}/{md5}"), ignore_errors=True)

    def _delete_user_images(self, user_id: str):
        import shutil
        shutil.rmtree(get_data_path(f"extracted_images/{user_id}"), ignore_errors=True)
