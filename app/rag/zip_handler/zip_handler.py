"""压缩包处理 — 解压 → 并行调用全局公共文档管道 → 聚合结果。"""
import os
import asyncio
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from app.config.loader import get_config
from app.utils.path_tool import get_data_path
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


def _get_allow_types() -> set[str]:
    return set(get_config("allow_knowledge_file_types", ["txt", "pdf", "md", "pptx", "docx"]))


def _get_max_workers() -> int:
    return int(os.getenv("ZIP_MAX_WORKERS", "4"))


class ZipTaskManager:
    """压缩包任务管理器：解压 → 全局公共文档管道 → 聚合结果。"""

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self._executor = ThreadPoolExecutor(max_workers=_get_max_workers())

    def create_task(self, file_path: Path, user_id: str) -> str:
        """创建压缩包处理任务，返回 task_id（前端轮询）。"""
        task_id = f"zip_{uuid.uuid4().hex[:12]}"
        self.tasks[task_id] = {
            "status": "pending",
            "progress": {"total": 0, "success": 0, "skipped": 0, "failed": 0, "pending": 0},
            "error_details": [],
        }
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._process(task_id, file_path, user_id))
        except RuntimeError:
            asyncio.run(self._process(task_id, file_path, user_id))
        return task_id

    async def _process(self, task_id: str, file_path: Path, user_id: str):
        """后台异步：解压 → 扫描 → 并行走公共管道 → 聚合 → 清理。"""
        tmp_dir = get_data_path(f"tmp/{task_id}")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        error_details = []
        any_success = False

        try:
            # 1. 解压
            self._extract(file_path, tmp_dir)
            logger.info(f"【压缩包】解压完成: {task_id}")

            # 2. 扫描过滤
            allow_types = _get_allow_types()
            all_files = list(tmp_dir.rglob("*"))
            valid_files = [f for f in all_files if f.is_file() and f.suffix.lstrip(".") in allow_types]
            skipped_files = [f for f in all_files if f.is_file() and f.suffix.lstrip(".") not in allow_types]

            # 解压后总大小检查
            max_total = int(os.getenv("MAX_ZIP_TOTAL_SIZE", "209715200"))
            total_size = sum(f.stat().st_size for f in valid_files)
            if total_size > max_total:
                self.tasks[task_id]["status"] = "failed"
                self.tasks[task_id]["error_details"] = [{
                    "file_path": file_path.name,
                    "error_type": "size_exceeded",
                    "reason": f"解压后文件总大小 {total_size // 1048576}MB 超过限制 {max_total // 1048576}MB",
                }]
                return

            progress = self.tasks[task_id]["progress"]
            progress["total"] = len(valid_files)
            progress["pending"] = len(valid_files)
            self.tasks[task_id]["status"] = "processing"

            # 不支持的格式记入跳过
            for f in skipped_files:
                error_details.append({
                    "file_path": str(f.relative_to(tmp_dir)),
                    "error_type": "unsupported_format",
                    "reason": f"不支持的文件格式: {f.suffix}",
                })
                progress["skipped"] += 1

            # 3. 并行走【全局公共复用文档管道】
            loop = asyncio.get_running_loop()
            futures = [
                loop.run_in_executor(self._executor, _process_file_through_shared_pipeline,
                                     f, user_id, tmp_dir)
                for f in valid_files
            ]
            results = await asyncio.gather(*futures, return_exceptions=True)

            # 4. 聚合结果
            for result in results:
                if isinstance(result, Exception):
                    progress["failed"] += 1
                    error_details.append({
                        "file_path": "unknown", "error_type": "parse_failed", "reason": str(result),
                    })
                elif isinstance(result, dict):
                    status = result.get("status", "failed")
                    if status == "done":
                        progress["success"] += 1
                        any_success = True
                    elif status == "duplicate":
                        progress["skipped"] += 1
                        error_details.append({
                            "file_path": result.get("file_path", ""),
                            "error_type": "duplicate",
                            "reason": "文件已有相同版本",
                        })
                    else:
                        progress["failed"] += 1
                        error_details.append({
                            "file_path": result.get("file_path", ""),
                            "error_type": result.get("error_type", "parse_failed"),
                            "reason": result.get("reason", "处理失败"),
                        })
                progress["pending"] = max(0, progress["pending"] - 1)

            self.tasks[task_id]["status"] = "completed"
            self.tasks[task_id]["error_details"] = error_details
            logger.info(
                f"【压缩包】处理完成: {task_id} | "
                f"成功 {progress['success']}/{progress['total']} 个文件"
                + (f", 跳过 {progress['skipped']} 个" if progress['skipped'] else "")
                + (f", 失败 {progress['failed']} 个" if progress['failed'] else "")
            )

        except Exception as e:
            self.tasks[task_id]["status"] = "failed"
            self.tasks[task_id]["error_details"] = [{
                "file_path": file_path.name, "error_type": "parse_failed",
                "reason": f"压缩包处理失败: {str(e)}",
            }]
            logger.error(f"【压缩包】处理失败: {task_id}, 原因: {e}")

        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            try:
                if file_path.exists():
                    file_path.unlink()
            except OSError:
                pass

            # 5. 任一文件成功入库则刷新 BM25 缓存
            if any_success:
                try:
                    from app.rag.retrievers.hybrid_retriever import HybridRetriever
                    HybridRetriever.invalidate_cache(user_id)
                except Exception:
                    pass

    def _extract(self, file_path: Path, dest: Path):
        """解压 zip / tar.gz / tar。"""
        suffix = file_path.suffix.lower()
        if suffix == ".zip":
            import zipfile
            with zipfile.ZipFile(file_path, "r") as zf:
                zf.extractall(dest)
        elif suffix in (".gz", ".tar") or ".tar" in file_path.name:
            import tarfile
            with tarfile.open(file_path, "r:*") as tf:
                tf.extractall(dest)
        else:
            raise ValueError(f"不支持的压缩格式: {suffix}")

    def get_task(self, task_id: str) -> dict | None:
        return self.tasks.get(task_id)


# ============================================================
# 全局公共复用文档管道 — 单入口（两条上传链路共用）
# ============================================================

_shared_processor = None


def _get_shared_processor():
    """获取共享的 DocumentProcessor 实例（懒加载单例）。"""
    global _shared_processor
    if _shared_processor is None:
        from app.rag.document_handler.processor import DocumentProcessor
        _shared_processor = DocumentProcessor()
    return _shared_processor


def _process_file_through_shared_pipeline(file_path: Path, user_id: str, base_dir: Path) -> dict:
    """通过全局公共复用文档管道处理单个文件（线程池中同步调用）。

    管道步骤：
    1. MD5 全局查重
    2. 文件格式分流（PDF/普通）
    3. 统一文本清洗
    4. 切片处理
    5. 向量入库（共用全局并发写入信号量）
    6. 写入 MD5 入库记录

    Returns:
        FileProcessResult 风格的 dict: {status, md5, file_path, chunks, error_type, reason}
    """
    import asyncio as _asyncio

    relative_path = str(file_path.relative_to(base_dir))
    processor = _get_shared_processor()

    try:
        result = _asyncio.run(processor.process(str(file_path), user_id, file_path.name))
        status = result.get("status", "failed")
        return {
            "status": status,
            "md5": result.get("md5", ""),
            "file_path": relative_path,
            "chunks": result.get("chunks", 0),
            "error_type": "" if status == "done" else _classify_error(status, result.get("reason", "")),
            "reason": result.get("reason", ""),
        }
    except Exception as e:
        return {
            "status": "failed",
            "md5": "",
            "file_path": relative_path,
            "chunks": 0,
            "error_type": "parse_failed",
            "reason": str(e),
        }


def _classify_error(status: str, reason: str) -> str:
    if status == "duplicate":
        return "duplicate"
    if "empty" in reason.lower() or "空" in reason:
        return "empty_content"
    if "size" in reason.lower() or "过大" in reason:
        return "size_exceeded"
    return "parse_failed"
