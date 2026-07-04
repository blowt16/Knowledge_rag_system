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
    return int(os.getenv("MAX_FILE_SIZE", "104857600"))


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
            from app.rag.chunk_batch_buffer import ChunkBatchBuffer
            buffer = ChunkBatchBuffer(user_id)
            result = await self._processor.process_to_chunks(
                file_path=tmp_path, user_id=user_id, original_filename=filename)

            status = result.get("status", "failed")
            if status in ("ok", "degraded"):
                buffer.add(result["chunks"], result["md5"], result["filename"], result.get("file_path", ""))
                buffer.final_flush()
                HybridRetriever.invalidate_cache(user_id)
                resp = {"status": status, "md5": result["md5"], "filename": filename, "chunks": len(result["chunks"])}
                if status == "degraded":
                    resp["degradation"] = result.get("degradation", {})
                return resp
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
        """原子化删除：先 ChromaDB（易失败）→ 后 MD5，ChromaDB 失败则 MD5 不动。"""
        records = self._md5_store.get_all_md5(user_id)
        target = next((r for r in records if r.get("md5") == md5), None)

        # 1. 先删 ChromaDB（失败则抛异常，MD5 未动，上层返回 500）
        self._vector_store.delete_by_md5(user_id, md5)

        # 2. 再删 MD5 记录（ChromaDB 已成功，JSONL 几乎不会失败）
        self._md5_store.delete_single_md5(user_id, md5)

        # 3. 清理图片（尽力而为）
        self._delete_image_directory(user_id, md5)
        HybridRetriever.invalidate_cache(user_id)

    def clear_user(self, user_id: str):
        """原子化清空：先 ChromaDB → 后 MD5，ChromaDB 失败则 MD5 不动。"""
        self._vector_store.delete_by_user(user_id)
        self._md5_store.clear_user(user_id)
        self._delete_user_images(user_id)
        HybridRetriever.invalidate_cache(user_id)

    def get_documents(self, user_id: str) -> list[dict]:
        """获取文档列表，以 ChromaDB 为准，MD5 存储双向同步。"""
        chroma_metadatas = self._vector_store.get_user_documents(user_id)

        # 按 MD5 去重（一个文档可能被切分为多个 chunk）
        seen_md5s = set()
        docs = []
        for meta in chroma_metadatas:
            md5 = meta.get("md5", "")
            if md5 and md5 not in seen_md5s:
                seen_md5s.add(md5)
                docs.append({
                    "md5": md5,
                    "original_filename": meta.get("original_filename", "未知"),
                    "upload_time": meta.get("created_at", ""),
                })

        md5_set = {r.get("md5") for r in self._md5_store.get_user_documents_info(user_id)}

        # 反向同步：ChromaDB 有但 MD5 存储缺失的，补写回去（确保去重检查准确）
        for doc in docs:
            if doc["md5"] not in md5_set:
                self._md5_store.save_md5_hex(
                    user_id, doc["md5"], doc["original_filename"]
                )
                logger.warning(
                    f"【一致性】补写缺失的 MD5 记录: {doc['original_filename']} ({doc['md5'][:12]}...)"
                )

        # 正向清理：MD5 存储有但 ChromaDB 不存在的，删除
        for md5 in md5_set:
            if md5 not in seen_md5s:
                self._md5_store.delete_single_md5(user_id, md5)
                logger.warning(f"【一致性】清理游离 MD5 记录: {md5[:12]}...")

        if md5_set != seen_md5s:
            HybridRetriever.invalidate_cache(user_id)

        return docs

    def _delete_image_directory(self, user_id: str, md5: str):
        import shutil
        shutil.rmtree(get_data_path(f"extracted_images/{user_id}/{md5}"), ignore_errors=True)

    def _delete_user_images(self, user_id: str):
        import shutil
        shutil.rmtree(get_data_path(f"extracted_images/{user_id}"), ignore_errors=True)
