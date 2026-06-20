"""压缩包处理 — 解压 → 并行解析 → 错误收集。"""
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
    """压缩包任务管理器：解压 → 并行解析 → 错误收集。"""

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self._executor = ThreadPoolExecutor(max_workers=_get_max_workers())

    def create_task(self, file_path: Path, user_id: str) -> str:
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
        tmp_dir = get_data_path(f"tmp/{task_id}")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        error_details = []

        try:
            self._extract(file_path, tmp_dir)
            logger.info(f"【压缩包】解压完成: {task_id}")

            allow_types = _get_allow_types()
            all_files = list(tmp_dir.rglob("*"))
            valid_files = [f for f in all_files if f.is_file() and f.suffix.lstrip(".") in allow_types]
            skipped_files = [f for f in all_files if f.is_file() and f.suffix.lstrip(".") not in allow_types]

            progress = self.tasks[task_id]["progress"]
            progress["total"] = len(valid_files)
            progress["pending"] = len(valid_files)
            self.tasks[task_id]["status"] = "processing"

            for f in skipped_files:
                rel = str(f.relative_to(tmp_dir))
                error_details.append({
                    "file_path": rel, "error_type": "unsupported_format",
                    "reason": f"不支持的文件格式: {f.suffix}",
                })
                progress["skipped"] += 1

            loop = asyncio.get_running_loop()
            futures = [
                loop.run_in_executor(self._executor, self._process_single_file, f, user_id, tmp_dir)
                for f in valid_files
            ]
            results = await asyncio.gather(*futures, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    progress["failed"] += 1
                    error_details.append({
                        "file_path": "unknown", "error_type": "parse_failed", "reason": str(result),
                    })
                elif isinstance(result, tuple):
                    file_path_str, res = result
                    if res.get("success"):
                        progress["success"] += 1
                    else:
                        progress["failed"] += 1
                        error_details.append({
                            "file_path": file_path_str,
                            "error_type": res.get("error_type", "parse_failed"),
                            "reason": res.get("reason", "未知错误"),
                        })
                progress["pending"] -= 1

            self.tasks[task_id]["status"] = "completed"
            self.tasks[task_id]["error_details"] = error_details

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

    def _process_single_file(self, file_path: Path, user_id: str, base_dir: Path) -> tuple:
        from app.rag.document_handler.processor import DocumentProcessor
        import asyncio as _asyncio

        relative_path = str(file_path.relative_to(base_dir))
        try:
            processor = DocumentProcessor()
            result = _asyncio.run(processor.process(str(file_path), user_id, file_path.name))
            if result.get("status") == "done":
                return (relative_path, {"success": True})
            elif result.get("status") == "duplicate":
                return (relative_path, {"success": False, "error_type": "duplicate", "reason": "文件已有相同版本"})
            else:
                return (relative_path, {
                    "success": False,
                    "error_type": _classify_error_str(result.get("reason", "")),
                    "reason": result.get("reason", "处理失败"),
                })
        except Exception as e:
            return (relative_path, {"success": False, "error_type": _classify_error_str(str(e)), "reason": str(e)})

    def _extract(self, file_path: Path, dest: Path):
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


def _classify_error_str(msg: str) -> str:
    msg_lower = msg.lower()
    if "md5" in msg_lower or "duplicate" in msg_lower or "已存在" in msg:
        return "duplicate"
    if "empty" in msg_lower or "空" in msg:
        return "empty_content"
    if "size" in msg_lower or "过大" in msg:
        return "size_exceeded"
    return "parse_failed"
