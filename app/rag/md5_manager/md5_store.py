"""MD5 去重存储 — JSON Lines 格式，按用户隔离。"""
import json
import os
from datetime import datetime
from pathlib import Path
from app.utils.path_tool import get_data_path
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


class MD5Store:
    """MD5 文件级去重存储。"""

    def __init__(self):
        self._base_dir = get_data_path("md5_hex_store")

    def _get_user_store_path(self, user_id: str) -> Path:
        user_dir = self._base_dir / "user_md5" / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir / "md5_hex_store.txt"

    def save_md5_hex(self, user_id: str, md5: str, original_filename: str, filename: str = "") -> None:
        """保存 MD5 记录。"""
        store_path = self._get_user_store_path(user_id)
        record = {
            "md5": md5,
            "filename": filename or original_filename,
            "original_filename": original_filename,
            "upload_time": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with open(store_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info(f"【向量数据库】文件 {original_filename} 的 md5 值 {md5} 已保存")

    def check_md5_exists(self, user_id: str, md5: str) -> bool:
        """检查 MD5 是否已存在。"""
        store_path = self._get_user_store_path(user_id)
        if not store_path.exists():
            return False
        with open(store_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("md5") == md5:
                        return True
                except json.JSONDecodeError:
                    continue
        return False

    def delete_single_md5(self, user_id: str, md5: str) -> bool:
        """删除单条 MD5 记录，文件为空时自动清理目录。"""
        store_path = self._get_user_store_path(user_id)
        if not store_path.exists():
            return False

        lines = []
        found = False
        with open(store_path, encoding="utf-8") as f:
            for line in f:
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                try:
                    record = json.loads(line_stripped)
                    if record.get("md5") == md5:
                        found = True
                        continue
                    lines.append(line)
                except json.JSONDecodeError:
                    lines.append(line)

        if found:
            if lines:
                with open(store_path, "w", encoding="utf-8") as f:
                    f.writelines(lines)
            else:
                store_path.unlink()
                user_dir = store_path.parent
                if user_dir.exists() and not any(user_dir.iterdir()):
                    user_dir.rmdir()

            logger.info(f"【向量数据库】已删除用户 {user_id} 的 MD5 记录: {md5}")
        return found

    def get_all_md5(self, user_id: str) -> list[dict]:
        """获取用户全部 MD5 记录。"""
        store_path = self._get_user_store_path(user_id)
        if not store_path.exists():
            return []

        records = []
        with open(store_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    def get_user_documents_info(self, user_id: str) -> list[dict]:
        """获取用户知识库文档概要信息（用于前端列表展示）。"""
        return self.get_all_md5(user_id)

    def clear_user(self, user_id: str):
        """清空用户所有 MD5 记录。"""
        store_path = self._get_user_store_path(user_id)
        if store_path.exists():
            store_path.unlink()
        user_dir = store_path.parent
        if user_dir.exists():
            import shutil
            shutil.rmtree(user_dir, ignore_errors=True)
        logger.info(f"【向量数据库】已清空用户 {user_id} 的所有 MD5 记录")
