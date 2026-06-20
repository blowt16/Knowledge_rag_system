"""知识库业务逻辑层。"""
import os
import hashlib
import tempfile
from pathlib import Path
from app.rag.document_handler.processor import DocumentProcessor
from app.rag.vector_store import VectorStoreService
from app.rag.md5_manager.md5_store import MD5Store
from app.rag.retrievers.hybrid_retriever import HybridRetriever
from app.utils.log_tool import get_logger
from app.utils.path_tool import get_data_path

logger = get_logger(__name__)

ALLOW_EXTENSIONS = {"txt", "pdf", "md", "pptx", "docx"}
ALLOW_MIME_TYPES = {
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/markdown": "md",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-powerpoint": "pptx",
}


class KnowledgeService:
    """知识库业务逻辑层。"""

    def __init__(self):
        self._processor = DocumentProcessor()
        self._vector_store = VectorStoreService()
        self._md5_store = MD5Store()

    async def upload_single(self, file_bytes: bytes, filename: str, user_id: str) -> dict:
        """处理单文件上传。

        Returns:
            {"status": "done"/"duplicate"/"failed", "md5": str, "filename": str, ...}
        """
        # 写入临时文件
        suffix = Path(filename).suffix
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        try:
            result = await self._processor.process(
                file_path=tmp_path,
                user_id=user_id,
                original_filename=filename,
            )

            if result.get("status") == "done":
                HybridRetriever.invalidate_cache(user_id)

            return result
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def validate_file(self, file_bytes: bytes, filename: str) -> dict:
        """文件校验：大小 + MIME 类型双重校验。

        Returns:
            {"valid": True/False, "error": str}
        """
        # 大小校验 (≤30MB)
        max_size = 30 * 1024 * 1024
        if len(file_bytes) > max_size:
            return {"valid": False, "error": f"文件大小超过限制（最大 30MB）"}

        # 扩展名校验
        ext = Path(filename).suffix.lower().lstrip(".")
        if ext not in ALLOW_EXTENSIONS:
            return {"valid": False, "error": f"不支持的文件格式: .{ext}"}

        # MIME 类型校验
        try:
            import magic
            mime_type = magic.from_buffer(file_bytes[:2048], mime=True)
            if mime_type in ALLOW_MIME_TYPES:
                return {"valid": True}
        except ImportError:
            pass

        # 双重校验：扩展名或 MIME 类型之一匹配即可
        if ext in ALLOW_EXTENSIONS:
            return {"valid": True}

        return {"valid": False, "error": f"文件类型校验失败"}

    def delete_by_md5(self, user_id: str, md5: str):
        """三层联动删除。"""
        self._vector_store.delete_by_md5(user_id, md5)
        self._md5_store.delete_single_md5(user_id, md5)
        self._delete_image_directory(user_id, md5)
        HybridRetriever.invalidate_cache(user_id)

    def clear_user(self, user_id: str):
        """清空用户全库。"""
        self._vector_store.delete_by_user(user_id)
        self._md5_store.clear_user(user_id)
        self._delete_user_images(user_id)
        HybridRetriever.invalidate_cache(user_id)

    def get_documents(self, user_id: str) -> list[dict]:
        """获取用户知识库文档列表。"""
        return self._md5_store.get_user_documents_info(user_id)

    def _delete_image_directory(self, user_id: str, md5: str):
        """删除 PDF 提取的图片缓存。"""
        import shutil
        img_dir = get_data_path(f"extracted_images/{user_id}/{md5}")
        shutil.rmtree(img_dir, ignore_errors=True)

    def _delete_user_images(self, user_id: str):
        """删除用户所有图片缓存。"""
        import shutil
        img_dir = get_data_path(f"extracted_images/{user_id}")
        shutil.rmtree(img_dir, ignore_errors=True)
