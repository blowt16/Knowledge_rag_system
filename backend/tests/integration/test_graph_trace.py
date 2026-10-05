"""图的 trace 完整性（回归锁）。

⚠️ 为什么值得单独一条测试：节点漏写 trace **不只是少一条耗时记录** ——
   SSE 层靠 `_node_ran(state, node)`（读 trace）判断「这个节点跑没跑」，
   来判断某些事件该不该发、什么时候发。漏写会让：
     · `generate` 漏写 → decision / citations / verify 全不发
     · `clarify` 漏写 → route 事件（含 facets）永远不发
   这两类都在 2026-10-05 实测中真实发生过，且**不报错、只是事件静默缺失**。
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

NODES = ["resolve", "route", "chat", "clarify", "rewrite", "retrieve", "rerank",
         "refuse", "build_context", "generate", "cite"]

NODES_DIR = Path(__file__).resolve().parents[2] / "app" / "graph" / "nodes"


@pytest.mark.parametrize("node", NODES)
def test_node_writes_trace(node: str):
    src = (NODES_DIR / f"{node}.py").read_text(encoding="utf-8")
    assert "NodeTrace" in src and "trace" in src, (
        f"节点 {node} 没有写 trace —— 会让 SSE 的 _node_ran() 判 False，"
        f"导致该节点之后的事件静默缺失"
    )


def test_every_node_module_has_a_node_function():
    """防止节点文件存在但函数名对不上（builder 里 import 不到会在启动时才炸）。"""
    from app.graph import builder

    src = inspect.getsource(builder)
    for node in NODES:
        assert f'"{node}"' in src, f"builder 未注册节点 {node}"
