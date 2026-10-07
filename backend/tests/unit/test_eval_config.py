"""消融开关语义（§5.3）与建 run 的两条路径（§5.4）。

⚠️ **空 config 与「全关的 config」不是一回事**，这是本文件盯的那条线：

| config | 落到哪条路径 | 实际跑什么 | 标签该是 |
|---|---|---|---|
| `{}` / `None` | `set_id`（批量评测页） | **线上完整链路** | 线上链路 |
| `{"suite": "full"}` | `suite`（消融页 / 脚本） | 纯向量基线 | 纯向量检索 |

老口径只看六个开关有没有开，`{}` 会被判成「六键全关」→ 显示「纯向量检索」，
而那一轮**跑的是线上链路** —— 列表里写着「纯向量检索」，看表的人当场误判。
"""

from __future__ import annotations

from app.eval import config as eval_config


def test_empty_config_means_online_pipeline():
    """不带 config 建 run = 线上完整链路（§5.4）。"""
    assert eval_config.label({}) == "线上链路"
    assert eval_config.label(None) == "线上链路"


def test_suite_config_is_still_the_vector_baseline():
    """老 `suite` 路径的 config 非空 → 仍是纯向量基线。

    这与它**实际跑的行为一致**（config 非空 → `switches()` 返回非 None →
    六个开关全关 → 纯向量检索），所以不改 —— 改了反而和历史对不上。
    """
    assert eval_config.label({"suite": "full"}) == "纯向量检索"
    assert eval_config.label({"suite": "refusal_calib"}) == "纯向量检索"


def test_switches_are_named_in_order():
    assert eval_config.label({"bm25": True, "suite": "full"}) == "开启 BM25"
    assert (eval_config.label({"rerank": True, "bm25": True, "suite": "full"})
            == "开启 BM25、精排")


def test_all_switches_on_is_the_full_pipeline():
    cfg = {k: True for k in eval_config.SWITCH_KEYS}
    assert eval_config.label(cfg) == "完整链路"


def test_switches_returns_none_for_online_traffic():
    """线上流量（`eval_config` 为空）→ None，图节点据此走默认路径。"""
    assert eval_config.switches({}) is None
    assert eval_config.switches({"eval_config": {}}) is None
    assert eval_config.switches({"eval_config": {"bm25": False}}) == {
        k: False for k in eval_config.SWITCH_KEYS}
