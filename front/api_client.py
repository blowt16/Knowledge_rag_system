"""后端 API 客户端 — 封装所有 HTTP 调用。"""
from __future__ import annotations

import json
from typing import Generator

import requests

from config import API_BASE_URL, USER_ID

# ============================================================
# 健康检查
# ============================================================


def check_health() -> bool:
    try:
        r = requests.get(f"{API_BASE_URL}/health", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


# ============================================================
# Chat — SSE 流式
# ============================================================


def send_chat_stream(
    query: str, session_id: str | None = None, user_id: str = USER_ID
) -> Generator[dict, None, None]:
    """流式发送聊天消息，逐行 yield SSE 事件字典。"""
    body = {"query": query, "session_id": session_id, "user_id": user_id, "stream": True}
    resp = requests.post(
        f"{API_BASE_URL}/chat",
        json=body,
        stream=True,
        timeout=120,
    )
    resp.raise_for_status()
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        data_str = line.removeprefix("data: ")
        if data_str.strip() == "[DONE]":
            break
        try:
            yield json.loads(data_str)
        except json.JSONDecodeError:
            continue


# ============================================================
# 知识库管理
# ============================================================


def upload_document(file_content: bytes, filename: str, user_id: str = USER_ID) -> dict:
    files = {"file": (filename, file_content)}
    data = {"user_id": user_id}
    r = requests.post(
        f"{API_BASE_URL}/knowledge/add/single",
        files=files,
        data=data,
        timeout=300,
    )
    r.raise_for_status()
    return r.json()


def list_documents(user_id: str = USER_ID) -> dict:
    r = requests.get(
        f"{API_BASE_URL}/knowledge/documents",
        params={"user_id": user_id},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def delete_document_by_md5(md5: str, user_id: str = USER_ID) -> dict:
    r = requests.delete(
        f"{API_BASE_URL}/knowledge/md5/delete/{md5}",
        params={"user_id": user_id},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def delete_document_by_filename(filename: str, user_id: str = USER_ID) -> dict:
    r = requests.delete(
        f"{API_BASE_URL}/knowledge/md5/{filename}",
        params={"user_id": user_id},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def clear_knowledge(user_id: str = USER_ID) -> dict:
    r = requests.delete(
        f"{API_BASE_URL}/knowledge/md5/clear",
        params={"user_id": user_id},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


# ============================================================
# 会话管理
# ============================================================


def create_conversation(user_id: str = USER_ID, title: str = "") -> dict:
    r = requests.post(
        f"{API_BASE_URL}/conversation/new",
        params={"user_id": user_id, "title": title},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def list_conversations(user_id: str = USER_ID) -> dict:
    r = requests.get(
        f"{API_BASE_URL}/conversation/list",
        params={"user_id": user_id},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def get_messages(session_id: str) -> dict:
    r = requests.get(
        f"{API_BASE_URL}/conversation/{session_id}/messages",
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def delete_conversation(session_id: str) -> dict:
    r = requests.delete(
        f"{API_BASE_URL}/conversation/{session_id}",
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def clear_conversations(user_id: str = USER_ID) -> dict:
    r = requests.delete(
        f"{API_BASE_URL}/conversation/clear/{user_id}",
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


# ============================================================
# 压缩包上传
# ============================================================


def upload_zip(file_content: bytes, filename: str, user_id: str = USER_ID) -> dict:
    files = {"file": (filename, file_content)}
    data = {"user_id": user_id}
    r = requests.post(
        f"{API_BASE_URL}/api/knowledge/upload_zip",
        files=files,
        data=data,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def get_zip_task_status(task_id: str) -> dict:
    r = requests.get(
        f"{API_BASE_URL}/api/knowledge/task/{task_id}",
        timeout=10,
    )
    r.raise_for_status()
    return r.json()
