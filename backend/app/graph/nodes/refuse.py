"""节点 11：refuse —— 拒答（§3.5.3 节点 11）。

```
候选为空（rerank 之后的条件边）
      ↓
固定话术："知识库中未找到相关依据"
+ 可附带提示（如"该类问题建议咨询教务处"）
      ↓
refused = true, refusal_reason = "no_candidate"
      ↓
记入 qa_logs
      ↓
     END
```

> 另一条拒答路径是 `generate` 判定证据不足后直接到 END
> （模型返回 `REFUSED_NO_EVIDENCE`），**不经过本节点** —— 那里用的是服务端固定文案。

**两条拒答路径在 SSE 上如何区分**（两条路都很容易实现成不一样，必须钉死）：

| 拒答来源 | `decision` 事件 | `refused` 事件 |
|---|---|---|
| 候选为空（`rerank` 后短路 → 本节点） | **不发送** | **发送**，`reason = "no_candidate"` |
| 模型判定证据不足（节点 9 → 直接 END） | **发送** `REFUSED_NO_EVIDENCE` | **发送**，`reason = "insufficient_evidence"` |

即 **`refused` 事件两条路都发**；`decision` 事件只在 `generate` 跑过之后才有意义。

**拒答数据的二次价值**：`qa_logs` 中 `is_refused = true` 的问题清单，
等于「学生问了但知识库答不上来」的清单 —— 管理端展示后可反哺知识库补充文档。
"""

from __future__ import annotations

from app.graph.state import RAGState

REFUSAL_TEXT = "知识库中未找到相关依据，无法回答该问题。"
REFUSAL_HINT = "该类问题建议咨询教务处或相关职能部门。"


async def refuse_node(state: RAGState) -> dict:
    import time

    from app.graph.state import NodeTrace

    started = time.perf_counter()
    return {
        "refused": True,
        "refusal_reason": "no_candidate",
        "answer": REFUSAL_TEXT,
        "citations": [],
        "decision": "",          # ⚠️ 本条路径**不发** decision 事件
        # ⚠️ 每个节点都必须写 trace —— 漏写不只是少一条耗时记录，
        #    还会让 SSE 那边 `_node_ran()` 判 False，导致该节点之后的事件不发
        "trace": [NodeTrace(node="refuse", ms=int((time.perf_counter() - started) * 1000),
                            recalled=0, degraded=None)],
    }
