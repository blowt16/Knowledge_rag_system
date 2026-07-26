"""单文件上传任务管理器 — 后台异步处理 + SSE 流式进度。"""
import asyncio
import os
import uuid
from pathlib import Path

from app.config.loader import get_config
from app.rag.document_handler.processor import DocumentProcessor
from app.rag.chunk_batch_buffer import ChunkBatchBuffer
from app.rag.retrievers.hybrid_retriever import HybridRetriever
from app.utils.log_tool import get_logger
from app.utils.path_tool import get_data_path

logger = get_logger(__name__)


class SingleUploadTracker:
    """单文件上传任务管理器，模式与 ZipTaskManager 一致。"""

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self._queues: dict[str, asyncio.Queue] = {}

    def _push_event(self, task_id: str, event: dict):
        q = self._queues.get(task_id)
        if q:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def create_task(self, file_bytes: bytes, filename: str, user_id: str) -> str:
        """保存文件并启动后台处理任务，立即返回 task_id。"""
        task_id = f"single_{uuid.uuid4().hex[:12]}"

        suffix = Path(filename).suffix
        tmp_dir = get_data_path(get_config("temp_upload_dir", "tmp/uploads"))
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / f"rag_upload_{task_id}{suffix}"
        tmp_path.write_bytes(file_bytes)

        self.tasks[task_id] = {
            "status": "pending",
            "filename": filename,
            "stage": "准备中",
        }
        self._queues[task_id] = asyncio.Queue(maxsize=get_config("sse_queue_maxsize", 64))

        loop = asyncio.get_running_loop()
        loop.create_task(self._process(task_id, tmp_path, filename, user_id))
        return task_id

    def get_stream(self, task_id: str) -> asyncio.Queue | None:
        return self._queues.get(task_id)

    def get_task(self, task_id: str) -> dict | None:
        """获取任务状态信息（用于前端轮询兜底）。"""
        return self.tasks.get(task_id)

    def cleanup(self, task_id: str):
        self._queues.pop(task_id, None)

    def cancel_all(self):
        """关闭时取消所有后台处理任务。"""
        for task_id in list(self.tasks.keys()):
            self.tasks[task_id]["status"] = "cancelled"
        self._queues.clear()
        logger.info(f"[SingleUploadTracker] cancelled {len(self.tasks)} tasks")

    async def _process(self, task_id: str, file_path: Path, filename: str, user_id: str):
        import time
        t_start = time.time()
        processor = DocumentProcessor()
        buffer = ChunkBatchBuffer(user_id)

        # 通过闭包保存 MD5 + 流水线状态，供 on_batch / on_progress / cleanup 使用
        _ctx = {
            "md5_hex": "",
            "extension": file_path.suffix.lower().lstrip("."),
            "on_batch_triggered": False,
        }

        # 预计算 MD5，确保 process_to_chunks 内部的 on_batch 回调能拿到正确 MD5
        import hashlib
        try:
            _ctx["md5_hex"] = hashlib.md5(file_path.read_bytes()).hexdigest()
        except Exception:
            pass

        try:
            def _cleanup_on_failure():
                """文件解析失败时清理所有残留数据：ChromaDB + 图片 + MD5。"""
                from app.rag.chunk_batch_buffer import cleanup_failed_embedding
                md5 = _ctx.get("md5_hex", "")
                if md5:
                    cleanup_failed_embedding(user_id, md5)
                    # MD5 记录可能已被 buffer._flush 写入，需额外清理
                    try:
                        from app.rag.md5_manager.md5_store import MD5Store
                        MD5Store().delete_single_md5(user_id, md5)
                    except Exception:
                        pass

            async def on_progress(stage: str, text: str):
                self.tasks[task_id]["stage"] = stage
                self._push_event(task_id, {"event": "stage", "data": text, "stage": stage})

            async def on_batch(batch_docs, batch_start: int, batch_end: int,
                               batch_idx: int = 0, total_batches: int = 1):
                """MinerU 分批回调：清洗 → 切分 → 缓冲 → 进度推送。"""
                _ctx["on_batch_triggered"] = True
                if not batch_docs:
                    return
                chunks = await processor._prepare_chunks(
                    batch_docs, user_id, _ctx["md5_hex"], filename, _ctx["extension"],
                )
                if not chunks:
                    return
                buffer.add(chunks, _ctx["md5_hex"], filename, str(file_path))
                # 进度: 分类完成(10%) 到 清洗前(45%)，按批次比例分配
                pct = 0.10 + (0.45 - 0.10) * ((batch_idx + 1) / total_batches)
                self._push_event(task_id, {
                    "event": "stage",
                    "data": f"MinerU 第{batch_idx + 1}/{total_batches}批完成 (第{batch_start}-{batch_end}页), {len(chunks)} chunks",
                    "stage": "loading",
                    "progress": pct,
                })

            # 1. 文档加载 + 清洗 + 切分
            result = await processor.process_to_chunks(
                file_path=str(file_path),
                user_id=user_id,
                original_filename=filename,
                progress_callback=on_progress,
                on_batch=on_batch,
            )

            status = result.get("status", "failed")
            _ctx["md5_hex"] = result.get("md5", "") or _ctx["md5_hex"]

            # 防御：流水线模式下 on_batch 从未触发但返回 ok → 实际加载失败
            if status == "ok" and not result.get("chunks") and not _ctx.get("on_batch_triggered"):
                logger.warning(
                    f"【单文件上传】流水线模式但 on_batch 未触发，判定为加载失败: {filename}"
                )
                status = "failed"

            if status == "duplicate":
                self.tasks[task_id]["status"] = "duplicate"
                logger.info(f"【上传完成】{filename}: 重复文件, 耗时 {time.time() - t_start:.1f}s")
                self._push_event(task_id, {
                    "event": "done", "data": {
                        "status": "duplicate",
                        "md5": result.get("md5", ""),
                        "filename": filename,
                    }
                })
                return

            if status == "failed":
                _cleanup_on_failure()
                diagnosis = result.get("diagnosis", {})
                self.tasks[task_id]["status"] = "failed"
                self._push_event(task_id, {
                    "event": "error",
                    "data": diagnosis.get("detail", result.get("reason", "处理失败")),
                })
                done_data = {
                    "status": "failed",
                    "filename": filename,
                    "reason": diagnosis.get("reason", result.get("reason", "")),
                    "detail": diagnosis.get("detail", result.get("reason", "处理失败")),
                    "suggestion": diagnosis.get("suggestion", ""),
                }
                self._push_event(task_id, {"event": "done", "data": done_data})
                return

            if status not in ("ok", "degraded"):
                _cleanup_on_failure()
                self.tasks[task_id]["status"] = "failed"
                self._push_event(task_id, {
                    "event": "error",
                    "data": result.get("reason", "处理失败"),
                })
                self._push_event(task_id, {"event": "done", "data": {
                    "status": "failed",
                    "filename": filename,
                    "reason": result.get("reason", ""),
                    "detail": result.get("reason", "处理失败"),
                    "suggestion": "",
                }})
                return

            md5_hex = result["md5"]
            fp = result.get("file_path", "")

            # 2. 批量嵌入（流水线模式下 chunks 已在 on_batch 回调中缓冲）
            chunks = result.get("chunks", [])
            if not chunks:
                # 流水线模式：chunks 已在 on_batch 中缓冲，直接刷尾批
                self._push_event(task_id, {
                    "event": "stage", "data": "向量嵌入中…", "stage": "embedding",
                })
            else:
                # 传统模式：chunks 一次性加入缓冲池
                self._push_event(task_id, {
                    "event": "stage", "data": f"向量嵌入中 ({len(chunks)} chunks)…", "stage": "embedding",
                })
                buffer.add(chunks, md5_hex, filename, fp)
            buffer.final_flush()

            # 批量嵌入失败 → 进入文件解析失败处理流程
            if buffer.has_failures:
                from app.rag.chunk_batch_buffer import cleanup_failed_embedding
                cleanup_failed_embedding(user_id, md5_hex)
                logger.error(
                    f"【单文件上传】向量嵌入失败: {filename} (md5={md5_hex[:12]}...), "
                    f"失败批次={buffer.failed_batches}"
                )
                self.tasks[task_id]["status"] = "failed"
                self._push_event(task_id, {
                    "event": "error",
                    "data": "向量嵌入失败，请稍后重试",
                })
                self._push_event(task_id, {"event": "done", "data": {
                    "status": "failed",
                    "filename": filename,
                    "reason": "embedding_failed",
                    "detail": "向量嵌入失败，请稍后重试",
                    "suggestion": "请稍后重试，如持续失败请联系管理员",
                }})
                return

            HybridRetriever.invalidate_cache(user_id)

            is_degraded = status == "degraded"
            self.tasks[task_id]["status"] = "degraded" if is_degraded else "done"
            elapsed = time.time() - t_start
            logger.info(
                f"【上传完成】{filename}: {len(chunks)} chunks, "
                f"耗时 {elapsed:.1f}s ({elapsed/60:.1f}min)"
                + (" (降级模式)" if is_degraded else "")
            )
            done_data = {
                "status": "degraded" if is_degraded else "done",
                "md5": md5_hex,
                "filename": filename,
                "chunks": len(chunks),
                "elapsed_seconds": round(elapsed, 1),
            }
            if is_degraded:
                done_data["degradation"] = result.get("degradation", {})
            self._push_event(task_id, {"event": "done", "data": done_data})

        except Exception as e:
            logger.error(f"【单文件上传】处理失败 {filename}: {e}")
            _cleanup_on_failure()
            self.tasks[task_id]["status"] = "failed"
            self._push_event(task_id, {"event": "error", "data": str(e)})
            self._push_event(task_id, {"event": "done", "data": {"status": "failed", "reason": str(e)}})
        finally:
            try:
                os.unlink(file_path)
            except OSError:
                pass


_single_upload_tracker: SingleUploadTracker | None = None


def get_single_upload_tracker() -> SingleUploadTracker:
    global _single_upload_tracker
    if _single_upload_tracker is None:
        _single_upload_tracker = SingleUploadTracker()
    return _single_upload_tracker
