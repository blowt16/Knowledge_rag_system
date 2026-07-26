"""后端 API 客户端 — 封装所有 HTTP 调用。"""
from __future__ import annotations

import json
import time
from typing import Generator

import requests

from config import (
    API_BASE_URL, USER_ID,
    SSE_UPLOAD_TIMEOUT, SSE_CHAT_TIMEOUT,
    UPLOAD_SYNC_TIMEOUT, UPLOAD_ASYNC_TIMEOUT,
    DEFAULT_TIMEOUT, HEALTH_TIMEOUT,
)

# SSE 流断线重连次数（仅超时/网络错误，非 HTTP 错误）
_SSE_MAX_RETRIES = 3
_SSE_RETRY_DELAY = 3  # 秒


# ============================================================
# 健康检查
# ============================================================


def check_health() -> bool:
    try:
        r = requests.get(f"{API_BASE_URL}/health", timeout=HEALTH_TIMEOUT)
        return r.status_code == 200
    except Exception:
        return False


# ============================================================
# Chat — SSE 流式
# ============================================================


def send_chat_stream(
    query: str, session_id: str | None = None, user_id: str = USER_ID, mode: str = "agent"
) -> Generator[dict, None, None]:
    """流式发送聊天消息，逐行 yield SSE 事件字典。"""
    body = {"query": query, "session_id": session_id, "user_id": user_id, "stream": True, "mode": mode}
    resp = requests.post(
        f"{API_BASE_URL}/chat",
        json=body,
        stream=True,
        timeout=SSE_CHAT_TIMEOUT,
    )
    resp.raise_for_status()
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        data_str = line.removeprefix("data: ")
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
        timeout=UPLOAD_SYNC_TIMEOUT,
    )
    if not r.ok:
        try:
            detail = r.json().get("message", r.text)
        except Exception:
            detail = r.text
        raise requests.HTTPError(f"{r.status_code} {detail}", response=r)
    return r.json()


def upload_document_stream(file_content: bytes, filename: str, user_id: str = USER_ID) -> str:
    """上传单文件到后台任务，返回 task_id。"""
    files = {"file": (filename, file_content)}
    data = {"user_id": user_id}
    r = requests.post(
        f"{API_BASE_URL}/knowledge/single/upload",
        files=files,
        data=data,
        timeout=UPLOAD_ASYNC_TIMEOUT,
    )
    if not r.ok:
        try:
            detail = r.json().get("message", r.text)
        except Exception:
            detail = r.text
        raise requests.HTTPError(f"{r.status_code} {detail}", response=r)
    return r.json()["data"]["task_id"]


def _stream_sse_with_retry(
    url: str, timeout: int, poll_url: str | None = None,
) -> Generator[dict, None, None]:
    """SSE 流式读取，超时/网络错误自动重连，后端 timeout 事件也触发重连。

    重连耗尽后降级为轮询 poll_url（如果提供）。
    """
    last_exception = None
    for attempt in range(_SSE_MAX_RETRIES):
        try:
            resp = requests.get(url, stream=True, timeout=timeout)
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                data_str = line.removeprefix("data: ")
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                # 后端空闲超时 → 重连，不当作错误
                if event.get("event") == "timeout":
                    if attempt < _SSE_MAX_RETRIES - 1:
                        break  # 跳出 iter_lines → 外层 for 循环重试
                    yield event  # 最后一次，传给调用方处理
                    return
                yield event
                if event.get("event") in ("done", "error"):
                    return
            else:
                return  # resp.iter_lines 正常结束
        except (requests.ReadTimeout, requests.ConnectionError, requests.Timeout) as e:
            last_exception = e
        except Exception:
            raise
        if attempt < _SSE_MAX_RETRIES - 1:
            time.sleep(_SSE_RETRY_DELAY)

    # SSE 重连全部失败 → 降级为轮询
    if poll_url:
        yield from _poll_task_status(poll_url)
    elif last_exception:
        raise last_exception


def _poll_task_status(poll_url: str, interval: int = 5, max_wait: int = 1800) -> Generator[dict, None, None]:
    """轮询任务状态（SSE 重连失败后的兜底）。"""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            r = requests.get(poll_url, timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            data = r.json().get("data", {})
            status = data.get("status", "unknown")
            if status in ("done", "degraded"):
                yield {"event": "stage", "data": f"处理完成 ({data.get('filename', '')})", "stage": "embedding"}
                yield {"event": "done", "data": {"status": status, "filename": data.get("filename", ""), "chunks": 0}}
                return
            elif status == "failed":
                yield {"event": "error", "data": "文件解析失败，请重试"}
                return
            elif status == "duplicate":
                yield {"event": "done", "data": {"status": "duplicate"}}
                return
            yield {"event": "stage", "data": f"⏳ 仍在处理中… ({data.get('stage', '')})", "stage": "loading"}
        except Exception:
            pass
        time.sleep(interval)
    yield {"event": "error", "data": "处理超时，请稍后重试或联系管理员"}


def stream_single_progress(task_id: str) -> Generator[dict, None, None]:
    """SSE 流式获取单文件处理进度，超时自动重连，重连失败降级轮询。"""
    url = f"{API_BASE_URL}/knowledge/single/task/{task_id}/stream"
    poll_url = f"{API_BASE_URL}/knowledge/single/task/{task_id}"
    yield from _stream_sse_with_retry(url, SSE_UPLOAD_TIMEOUT, poll_url=poll_url)


def list_documents(user_id: str = USER_ID) -> dict:
    r = requests.get(
        f"{API_BASE_URL}/knowledge/documents",
        params={"user_id": user_id},
        timeout=DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def delete_document_by_md5(md5: str, user_id: str = USER_ID) -> dict:
    r = requests.delete(
        f"{API_BASE_URL}/knowledge/md5/delete/{md5}",
        params={"user_id": user_id},
        timeout=DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def delete_document_by_filename(filename: str, user_id: str = USER_ID) -> dict:
    r = requests.delete(
        f"{API_BASE_URL}/knowledge/md5/{filename}",
        params={"user_id": user_id},
        timeout=DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def clear_knowledge(user_id: str = USER_ID) -> dict:
    r = requests.delete(
        f"{API_BASE_URL}/knowledge/md5/clear",
        params={"user_id": user_id},
        timeout=DEFAULT_TIMEOUT,
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
        timeout=DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def list_conversations(user_id: str = USER_ID, offset: int = 0, limit: int = 20) -> dict:
    r = requests.get(
        f"{API_BASE_URL}/conversation/list",
        params={"user_id": user_id, "offset": offset, "limit": limit},
        timeout=DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def toggle_pin(session_id: str, is_top: bool) -> dict:
    r = requests.post(
        f"{API_BASE_URL}/conversation/{session_id}/pin",
        params={"is_top": is_top},
        timeout=DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def get_messages(session_id: str) -> dict:
    r = requests.get(
        f"{API_BASE_URL}/conversation/{session_id}/messages",
        timeout=DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def delete_conversation(session_id: str) -> dict:
    r = requests.delete(
        f"{API_BASE_URL}/conversation/{session_id}",
        timeout=DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def clear_conversations(user_id: str = USER_ID) -> dict:
    r = requests.delete(
        f"{API_BASE_URL}/conversation/clear/{user_id}",
        timeout=DEFAULT_TIMEOUT,
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
        timeout=UPLOAD_ASYNC_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def get_zip_task_status(task_id: str) -> dict:
    r = requests.get(
        f"{API_BASE_URL}/api/knowledge/task/{task_id}",
        timeout=DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def stream_zip_progress(task_id: str) -> Generator[dict, None, None]:
    """SSE 流式获取压缩包处理进度，超时自动重连，重连失败降级轮询。"""
    url = f"{API_BASE_URL}/api/knowledge/task/{task_id}/stream"
    poll_url = f"{API_BASE_URL}/api/knowledge/task/{task_id}"
    yield from _stream_sse_with_retry(url, SSE_UPLOAD_TIMEOUT, poll_url=poll_url)
