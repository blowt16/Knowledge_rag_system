"""提示词加载（方案 §3.1：提示词放 `app/config/prompts/`）。

⚠️ **为什么用 `string.Template`（`$name`）而不是 `str.format`（`{name}`）**：
   提示词正文里有大量 JSON 字面量（`{"decision":"ANSWERED",...}`），
   而 `str.format` 会把它们当成占位符 —— 必须**双写**成 `{{"decision":...}}`，
   照抄提示词的人一不小心就漏，运行时才炸 `KeyError`。
   2026-10-05 实测踩过（三处 prompt 同时中招）。

   `Template` 用 `$` 作前缀，花括号可以照常写单括号。**编辑提示词文件时
   不需要任何转义**，这也是 M5 校准小集「改一次提示词就能跑一轮」的前提。

⚠️ 提示词从节点文件搬到 config 下，就是为了 M5 的提示词调优 ——
   散在 6 个节点文件里改起来别扭，集中放一处才能快速迭代。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from string import Template

from app.core.config import BACKEND_DIR

logger = logging.getLogger(__name__)

PROMPTS_DIR = BACKEND_DIR / "app" / "config" / "prompts"


@lru_cache(maxsize=64)
def load_prompt(name: str) -> Template:
    """按名加载提示词模板（带缓存）。改文件后需重启或调 `reload_prompts()`。"""
    path = PROMPTS_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(
            f"提示词 {name!r} 不存在（找的是 {path}）。"
            f"可用：{[p.stem for p in PROMPTS_DIR.glob('*.txt')]}"
        )
    return Template(_strip_leading_comments(path.read_text(encoding="utf-8")))


def _strip_leading_comments(text: str) -> str:
    """去掉文件**开头**的 `#` 注释块 —— 那是给人看的，不该进提示词。

    ⚠️ 不处理的话有两个实际后果（2026-10-05 M2 实测）：
       ① 注释里的 `$name` 是**举例说明占位符语法**用的，而 `Template` 把它当成
          真占位符 → 每次渲染都告警「缺少变量 ['name']」。这条告警本是用来看
          「真忘了传 $query」的，天天响就等于不响。
       ② 注释原文会连同正文一起发给模型，白占 token，还把「模板语法说明」
          塞进了提示词正文。
    """
    lines = text.splitlines()
    i = 0
    while i < len(lines) and lines[i].lstrip().startswith("#"):
        i += 1
    return "\n".join(lines[i:]).lstrip("\n")


def render(name: str, **kwargs: object) -> str:
    """加载并填充提示词。

    用 `safe_substitute`：万一漏传变量，正文里原样留着 `$xxx` 而不是抛异常 ——
    宁可让模型看到一句奇怪的占位符，也不要让整条请求 500。
    """
    text = load_prompt(name).safe_substitute(**kwargs)
    missing = [k for k in _placeholders(load_prompt(name)) if k not in kwargs]
    if missing:
        logger.warning("提示词 %s 缺少变量 %s（正文里会原样留着 $xxx）", name, missing,
                       extra={"event": "prompt.missing_vars", "node": name})
    return text


def _placeholders(template: Template) -> set[str]:
    return {
        match.group("named") or match.group("braced")
        for match in template.pattern.finditer(template.template)
        if match.group("named") or match.group("braced")
    }


def reload_prompts() -> None:
    """清缓存（改完提示词文件后可热重载）。"""
    load_prompt.cache_clear()


def available() -> list[str]:
    return sorted(p.stem for p in PROMPTS_DIR.glob("*.txt"))
