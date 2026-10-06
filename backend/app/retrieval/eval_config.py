"""消融实验的开关（§5.3）。

**一句话**：`state["eval_config"]` 为空 → 一切走默认（线上行为，一个字节都不变）；
非空 → 按里面的开关逐项打开/关闭。

## 8 行叠加表与 6 个开关的对应（§5.3）

    行 1 纯向量        六键全 off
    行 2 +BM25         bm25
    行 3 +RRF          bm25 + rrf
    行 4 +精排          bm25 + rrf + rerank
    行 5 +verbatim     … + expand_verbatim
    行 6 +keywords     … + expand_keywords
    行 7 +hyde         … + expand_hyde
    行 8 完整链路       六键全 on

这个对应关系不是我编的：`eval_runs.config` 的注释（M0 建表时写的）就点了这
六个键，并明说 `rrf_weighted` / `resolve` **不对应叠加表行**。

## 两处口径要写清楚（文档没有）

1. **`rrf` 关掉时两路怎么合**：§5.3 没写。这里用「**min-max 归一化后相加**」
   —— 一个常见的无 RRF 融合基线。**这是消融脚手架，不是产品语义**，
   报告里会标明。
2. **`expand_*` 全关时不跑 rewrite**：直接用 `resolved_query` 作唯一查询。
   （第 5 行起 rewrite 才参与；由于 verbatim 的文本被强制等于 `resolved_query`，
   **第 4 行与第 5 行在单轮题上必然几乎相同** —— 这不是 bug，是「verbatim 即
   基线行为」的诚实结果，表里会注明。）
"""

from __future__ import annotations

SWITCH_KEYS = ("bm25", "rrf", "rerank",
               "expand_verbatim", "expand_keywords", "expand_hyde")


def switches(state: dict) -> dict[str, bool] | None:
    """取开关字典。**返回 None 表示「不是消融运行」**（线上）。"""
    raw = (state or {}).get("eval_config")
    if not raw:
        return None
    return {k: bool(raw.get(k, False)) for k in SWITCH_KEYS}


def on(state: dict, key: str, default: bool = True) -> bool:
    """某个开关是否打开。非消融运行时返回 `default`（线上一律走默认）。"""
    sw = switches(state)
    if sw is None:
        return default
    return sw.get(key, default)


def is_ablation(state: dict) -> bool:
    return switches(state) is not None


def normalized(config: dict | None) -> dict[str, bool]:
    """把任意 config 规整成六个键的布尔字典（缺的补 False）。"""
    src = config or {}
    return {k: bool(src.get(k, False)) for k in SWITCH_KEYS}


def label(config: dict | None) -> str:
    """**服务端派生**的配置标签（§4.3.1.4）。

    ⚠️ 由服务端派生而不是前端拼：开关命名会演进，前端各拼各的就会出现
       同一个配置在不同次运行里叫不同名字，对比表直接失效。
    """
    sw = normalized(config)
    if not any(sw.values()):
        return "纯向量检索"
    if all(sw.values()):
        return "完整链路"

    # 按**开启项**命名，读法与 §5.3 叠加表一致（「+BM25」「+BM25、RRF、精排」）。
    # 反面写法（「关闭 A、B、C、D、E」）在中间几行会长到读不出来。
    order = [("bm25", "BM25"), ("rrf", "RRF"), ("rerank", "精排"),
             ("expand_verbatim", "verbatim 查询"), ("expand_keywords", "keywords 查询"),
             ("expand_hyde", "hyde 查询")]
    enabled = [name for key, name in order if sw[key]]
    return "开启 " + "、".join(enabled)
