"""提示词加载 —— 占位符与注释（M2 实测发现的两处问题）。

⚠️ 为什么值得单独一条测试：提示词是**字符串**，错了不会报错 ——
   - 占位符少传一个 → `safe_substitute` 原样留着 `$query` **发给模型**，
     模型看到一句「$query」照答不误，只是答的是别的问题；
   - 文件开头的注释块会一起发给模型（含「$name 语法说明」这类元信息）。

   两者都只在真跑时看得出来，所以用测试锁住：**占位符集合**与**注释不落地**。
"""

from __future__ import annotations

import logging

import pytest

from app.core import prompts

# 每个提示词文件的占位符集合 —— 节点里传什么，这里就该是什么。
# 改提示词时若新增了 $xxx，这条会失败，提醒你去节点里把值传上。
EXPECTED_PLACEHOLDERS = {
    "resolve": {"history", "query"},
    "route": {"last_route", "query"},
    "clarify": {"history", "query"},
    "rewrite": {"query"},
    "chat": {"query"},
    "generate": {"history", "context", "query"},
}


def test_every_prompt_is_covered_by_this_test():
    """新增提示词文件时，必须同时在这里登记占位符。"""
    assert set(prompts.available()) == set(EXPECTED_PLACEHOLDERS)


@pytest.mark.parametrize("name", sorted(EXPECTED_PLACEHOLDERS))
def test_placeholder_set_matches_expectation(name):
    got = prompts._placeholders(prompts.load_prompt(name))
    assert got == EXPECTED_PLACEHOLDERS[name], (
        f"{name}.txt 的占位符是 {got}，节点里预期传 {EXPECTED_PLACEHOLDERS[name]} —— "
        f"多出来的会原样发给模型，少了的会让模型收到一句 $xxx"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_PLACEHOLDERS))
def test_rendered_prompt_has_no_leftover_placeholder(name, caplog):
    """按节点的方式传全变量渲染 → 不留 `$xxx`、也不该有「缺少变量」告警。"""
    values = {k: f"<{k}>" for k in EXPECTED_PLACEHOLDERS[name]}
    with caplog.at_level(logging.WARNING, logger="app.core.prompts"):
        text = prompts.render(name, **values)

    assert "$" not in text, "渲染后仍有 $ —— 有占位符没被替换，模型会看到 $xxx"
    assert not [r for r in caplog.records if "缺少变量" in r.getMessage()]


@pytest.mark.parametrize("name", sorted(EXPECTED_PLACEHOLDERS))
def test_header_comments_never_reach_the_model(name):
    """文件开头的 `#` 注释是给人看的，不该发给模型。"""
    values = {k: "X" for k in EXPECTED_PLACEHOLDERS[name]}
    text = prompts.render(name, **values)

    assert not text.lstrip().startswith("#"), "注释块被当成提示词发出去了"
    assert "string.Template" not in text


def test_missing_variable_is_still_reported(caplog):
    """反向锁：真漏传变量时，那条告警**必须还在**（别把护栏一起删了）。"""
    with caplog.at_level(logging.WARNING, logger="app.core.prompts"):
        prompts.render("rewrite")     # 故意不传 query

    assert any("缺少变量" in r.getMessage() for r in caplog.records)
