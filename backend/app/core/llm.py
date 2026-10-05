"""LLM 客户端（DeepSeek 官方 API，非思考模式）。

口径见施工计划 §1.0：
  - 模型 `deepseek-flash`（官方 id，**不是**已退役的 `deepseek-chat` 别名）
  - `reasoning_effort: "none"` 关思考 —— 实测 `prompt_tokens` 36→10、
    `reasoning_content` 归零，与旧别名行为逐项一致
  - 关思考是**必须的，不是优化**：思考模式下 `content` 可能为空，
    而节点 9 的流式解析器只认 `content` 里的 JSON

⚠️ `enable_thinking: false` **不生效**（实测），别用它。
   备选写法 `thinking: {"type": "disabled"}` 同样实测生效。
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from app.core.config import cfg, secret

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMTimeout(LLMError):
    """超时 —— 各节点的降级分支靠它区分「失败」与「正常返回」。"""


def _base() -> str:
    return secret("llm.deepseek_base_url").rstrip("/")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {secret('llm.deepseek_api_key')}",
        "Content-Type": "application/json",
    }


def _model() -> str:
    return cfg("llm.model", "deepseek-flash")


def _apply_thinking(body: dict[str, Any]) -> dict[str, Any]:
    """关思考模式。主用 reasoning_effort，配置里可切到 thinking 写法作后备。"""
    effort = cfg("llm.reasoning_effort", "none")
    if effort:
        body["reasoning_effort"] = effort
    return body


async def complete(
    messages: list[dict[str, str]],
    *,
    timeout: float = 30.0,
    max_tokens: int | None = None,
    temperature: float | None = None,
    json_mode: bool = False,
) -> str:
    """非流式补全，返回 content 文本。"""
    body: dict[str, Any] = {
        "model": _model(),
        "messages": messages,
        "max_tokens": max_tokens or int(cfg("llm.max_output_tokens_runtime", 8192)),
        "temperature": (temperature if temperature is not None
                        else float(cfg("llm.temperature", 0.3))),
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    _apply_thinking(body)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(_base() + "/chat/completions",
                                  headers=_headers(), json=body)
    except httpx.TimeoutException as e:
        raise LLMTimeout(str(e)) from e
    except httpx.HTTPError as e:
        raise LLMError(str(e)) from e

    if r.status_code != 200:
        raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")

    data = r.json()
    try:
        return data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError) as e:
        raise LLMError(f"响应结构异常：{str(data)[:200]}") from e


async def complete_json(
    messages: list[dict[str, str]],
    *,
    timeout: float = 30.0,
    max_tokens: int | None = None,
) -> Any:
    """要求 JSON 输出并解析。解析失败抛 LLMError —— 调用方按各自的降级链处理。"""
    raw = await complete(messages, timeout=timeout, max_tokens=max_tokens)
    text = _strip_fence(raw)
    try:
        return json.loads(text)
    except ValueError as e:
        raise LLMError(f"JSON 解析失败：{raw[:200]}") from e


def _strip_fence(text: str) -> str:
    """去掉 BOM / 前导空白 / ```json 围栏。

    节点 9 的首字符校验用同一套清理（§3.5.3 节点 9 第 4 条）。
    """
    text = text.lstrip("﻿").strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


async def stream_raw(
    messages: list[dict[str, str]],
    *,
    timeout: float = 180.0,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> AsyncIterator[str]:
    """流式补全，逐个产出 **content 增量**（不含 reasoning）。

    ⚠️ 只产出 `content` 的 delta：思考模式下 `reasoning_content` 是另一个字段，
       混进来会让节点 9 的 JSON 解析器看到非 JSON 内容。
    """
    body: dict[str, Any] = {
        "model": _model(),
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens or int(cfg("llm.max_output_tokens_runtime", 8192)),
        "temperature": (temperature if temperature is not None
                        else float(cfg("llm.temperature", 0.3))),
    }
    _apply_thinking(body)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", _base() + "/chat/completions",
                                     headers=_headers(), json=body) as response:
                if response.status_code != 200:
                    detail = (await response.aread())[:200]
                    raise LLMError(f"HTTP {response.status_code}: {detail!r}")
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except ValueError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield piece
    except httpx.TimeoutException as e:
        raise LLMTimeout(str(e)) from e
    except httpx.HTTPError as e:
        raise LLMError(str(e)) from e


def is_configured() -> bool:
    import os
    return bool(os.getenv(secret("llm.deepseek_api_key")))
