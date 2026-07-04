"""Chunk 批量嵌入缓冲池 — 双阈值刷批 + 指数退避重试。"""
import time
import threading
from app.config.loader import get_config
from app.rag.vector_store import VectorStoreService
from app.rag.md5_manager.md5_store import MD5Store
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


def _cfg(key, default):
    return get_config(key, default)


class ChunkBatchBuffer:
    """Chunk 缓冲池：数量 + 总字符双阈值触发批量嵌入，失败自动重试。"""

    def __init__(self, user_id: str):
        self._user_id = user_id
        self._buffer: list = []
        self._total_chars = 0
        self._md5_records: list[tuple[str, str, str]] = []  # (md5, filename, file_path)
        self._lock = threading.Lock()

        self._max_count = _cfg("batch_max_count", 50)
        self._max_chars = _cfg("batch_max_chars", 18000)
        self._sleep_ms = _cfg("batch_sleep_ms", 100)
        self._max_retries = _cfg("batch_max_retries", 3)
        self._backoff = _cfg("batch_retry_backoff", [1, 2, 4])

        self._total_flushed = 0
        self._failed_batches = 0
        self._vector_store = VectorStoreService()
        self._md5_store = MD5Store()

    def add(self, chunks: list, md5_hex: str, filename: str, file_path: str = "") -> bool:
        """添加一批 chunk 到缓冲池，达到阈值自动刷批。返回是否有失败。"""
        if not chunks:
            return False
        batch = None
        batch_md5s = None
        with self._lock:
            self._buffer.extend(chunks)
            self._total_chars += sum(len(d.page_content) for d in chunks)
            self._md5_records.append((md5_hex, filename, file_path))
            if self._should_flush():
                batch, batch_md5s = self._extract_batch()
        if batch:
            return self._flush(batch, batch_md5s)
        return False

    def final_flush(self):
        """强制刷出缓冲内所有剩余 chunk（尾批）。"""
        with self._lock:
            if not self._buffer:
                return
            batch, batch_md5s = self._extract_batch()
        if batch:
            self._flush(batch, batch_md5s)

    def _should_flush(self) -> bool:
        return len(self._buffer) >= self._max_count or self._total_chars >= self._max_chars

    def _extract_batch(self) -> tuple[list, list[tuple[str, str, str]]]:
        batch = list(self._buffer)
        batch_md5s = list(self._md5_records)
        self._buffer.clear()
        self._total_chars = 0
        self._md5_records.clear()
        return batch, batch_md5s

    def _flush(self, batch: list, batch_md5s: list[tuple[str, str, str]]) -> bool:
        """执行单批嵌入 + 写入 + MD5 保存，失败按退避重试。"""
        batch_len = len(batch)
        batch_chars = sum(len(d.page_content) for d in batch)
        logger.info(f"【批量嵌入】刷批开始: {batch_len} chunks, {batch_chars} 字符")

        # chunk_id / chunk_index 已在 process_to_chunks() 中按文件内位置分配好，
        # 跨文件 md5 不同，id 天然唯一，此处不重分配以避免跨批冲突。

        for attempt in range(self._max_retries):
            try:
                self._vector_store.add_documents(batch)
                break
            except Exception as e:
                if attempt < self._max_retries - 1:
                    delay = self._backoff[attempt] if attempt < len(self._backoff) else 2 ** attempt
                    logger.warning(f"【批量嵌入】刷批失败 (尝试 {attempt + 1}/{self._max_retries}): {e}，{delay}s 后重试")
                    time.sleep(delay)
                else:
                    logger.error(f"【批量嵌入】刷批失败 ({self._max_retries} 次重试均失败): {e}")
                    self._failed_batches += 1
                    return False

        # 保存本批涉及的所有 MD5 记录
        seen = set()
        for md5_hex, filename, file_path in batch_md5s:
            if md5_hex not in seen:
                seen.add(md5_hex)
                try:
                    self._md5_store.save_md5_hex(self._user_id, md5_hex, filename, file_path)
                except Exception as e:
                    logger.error(f"【批量嵌入】MD5 保存失败 {filename}: {e}")

        self._total_flushed += batch_len
        logger.info(f"【批量嵌入】刷批完成: {batch_len} chunks, 累计 {self._total_flushed} chunks")
        time.sleep(self._sleep_ms / 1000.0)
        return True

    @property
    def total_flushed(self) -> int:
        return self._total_flushed

    @property
    def failed_batches(self) -> int:
        return self._failed_batches
