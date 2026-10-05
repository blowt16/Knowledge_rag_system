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


def _render_generate() -> str:
    """按节点的方式渲染 generate —— 占位符集合变了也不用手改这里。"""
    values = {k: f"<{k}>" for k in EXPECTED_PLACEHOLDERS["generate"]}
    return prompts.render("generate", **values)


# generate.txt 的四条硬约束（§3.5.3 节点 9）。每条的判据取**动词**那半句，
# 不取格式（格式会随排版变，约束不会）。
GENERATE_HARD_RULES = {
    "句级引用标记": ["每个结论句", "[n]"],
    "禁止只在末尾堆来源": ["禁止只在末尾堆来源"],
    "适用范围显式": ["适用版本"],
    "未答部分显式声明": ["资料中未找到", "不得用常识补全"],
    "历史与材料冲突以材料为准": ["以材料为准"],
}


@pytest.mark.parametrize("rule,needles", sorted(GENERATE_HARD_RULES.items()))
def test_generate_prompt_keeps_the_four_hard_rules(rule, needles):
    """四条硬约束是「模型必须逐条遵守」的部分 —— 掉一条不会报错，只会变差。"""
    text = _render_generate()
    missing = [n for n in needles if n not in text]
    assert not missing, f"generate.txt 少了「{rule}」的判据 {missing}"


def test_generate_prompt_keeps_injection_guard():
    """历史与证据是**数据不是指令** —— 注入防护不能省。"""
    assert "不是指令" in _render_generate()


def test_generate_contract_has_no_citation_numbers():
    """引用编号由服务端从正文派生，契约里**不能**再要模型给一份编号。"""
    assert "citation_numbers" not in _render_generate()


def test_missing_variable_is_still_reported(caplog):
    """反向锁：真漏传变量时，那条告警**必须还在**（别把护栏一起删了）。"""
    with caplog.at_level(logging.WARNING, logger="app.core.prompts"):
        prompts.render("rewrite")     # 故意不传 query

    assert any("缺少变量" in r.getMessage() for r in caplog.records)
