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

    def cleanup(self, task_id: str):
        self._queues.pop(task_id, None)

    async def _process(self, task_id: str, file_path: Path, filename: str, user_id: str):
        import time
        t_start = time.time()
        processor = DocumentProcessor()
        try:
            async def on_progress(stage: str, text: str):
                self.tasks[task_id]["stage"] = stage
                self._push_event(task_id, {"event": "stage", "data": text, "stage": stage})

            # 1. 文档加载 + 清洗 + 切分
            result = await processor.process_to_chunks(
                file_path=str(file_path),
                user_id=user_id,
                original_filename=filename,
                progress_callback=on_progress,
            )

            status = result.get("status", "failed")

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
                diagnosis = result.get("diagnosis", {})
                self.tasks[task_id]["status"] = "failed"
                self._push_event(task_id, {
                    "event": "error",
                    "data": diagnosis.get("detail", result.get("reason", "处理失败")),
                })
                # done 事件扁平化 diagnosis 信息，前端可直接读取 reason/detail
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

            chunks = result["chunks"]
            md5_hex = result["md5"]
            fp = result.get("file_path", "")

            # 2. 批量嵌入
            self._push_event(task_id, {
                "event": "stage", "data": f"向量嵌入中 ({len(chunks)} chunks)…", "stage": "embedding",
            })

            buffer = ChunkBatchBuffer(user_id)
            buffer.add(chunks, md5_hex, filename, fp)
            buffer.final_flush()

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
