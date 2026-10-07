"""评测模块（§4）。

评测相关的代码**集中在这里**，不再散落在 `services/` 与 `retrieval/`：

    config.py    消融开关语义（原来是 `retrieval/eval_config.py`）
    ragas.py     隔离环境子进程调用（原来是 `services/ragas_service.py`）
    runner.py    跑一轮（原来是 `services/eval_service.py`）
    sets.py      评测集 + 用例的增删改查
    generate.py  从文档自动生成用例
    report.py    报告组装

⚠️ 代价要记着：`config.py` 搬进来之后，三个**检索侧图节点**（rewrite / retrieve /
   rerank）从此依赖评测模块 —— 方向是反的（本来是检索开关语义，被图节点读）。
   这是负责人明确要求并已确认接受的（§4）。
"""
