"""从文档自动生成评测用例（§8）。

做法**沿用 `tools/make_eval_cases.py` 那套已经在用的**，不另发明：

    1. 从**向量库**取出这份文档的 chunk（带页码、片段号）—— 不是自己重切一遍
    2. 均匀挑 N 个片段（N = 生成条数，上限 10，默认 5）
    3. 每个片段问模型一次：出一道题 + 答案必须逐字抄自这一段
    4. 硬校验，不过就重试：
         ① 标准答案必须是这段原文的**连续子串**（忽略空白）
         ② 问句里**不许有代词**（这个/那个/该/其…）
    5. 通过的入库

**为什么必须从向量库取 chunk 而不是自己重切**：旧脚本读规范化正文、自己
`chunk_text()` 切一遍。但检索看到的是**入库时切的那份** —— 两套切法一旦不一致，
「来源片段」就是假的。从 Chroma 取还能白拿 `page` 与 `chunk_index`。

**为什么校验②是必须的**：旧脚本的实测教训 —— 单轮题带代词（「这个细则管的是
哪些学生？」）会被图的 `resolve` 节点**正确**判成「指代不明」→ 走 `clarify`，
于是评测里凭空多出「路由错」的假失败。代词是多轮题库在干的活儿。
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid

from app import db
from app.core import llm
from app.core.config import repo_path
from app.eval import sets
from app.retrieval import vector

logger = logging.getLogger(__name__)

#: 生成条数上限（决策 18）。再多就该走命令行批量跑，不该卡在 HTTP 请求里。
MAX_COUNT = 10
DEFAULT_COUNT = 5

#: 总超时（秒）。超时**返回部分结果**，不把已经生成的丢掉。
TOTAL_TIMEOUT = 120.0

#: 并发几路问模型。3 路时 5 条约 20~30 秒。
CONCURRENCY = 3

#: 太短的片段出不了像样的题（与旧脚本同口径）
MIN_CHUNK_CHARS = 120

#: 单段最多问几次（首次 + 重试 2 次）
MAX_ATTEMPTS = 3

FACTUAL_INSTRUCTION = ("出一道路人式的事实题：问某项规定/条件/时限是**什么**。"
                       "问法要像一个学生随口问的，不要照抄原文句式。")


class DocumentNotIndexed(LookupError):
    """这份文档在向量库里没有可用的片段 —— 接口层译成 409/400。"""


def squeeze(s: str) -> str:
    """去掉**所有空白**再比。用于「逐字」校验。

    ⚠️ 必须这么做：规范化正文里有 PDF 提取留下的硬换行与空行
    （实测：`…提出申请并经\\n\\n学院审核同意后送达；`），模型复述时自然写成
    一行 —— 用带空白的原串去比会判成「不是原文」，而**它确实是原文**。
    去空白比对仍是强保证：**非空白字符序列必须连续出现在原文里**，
    改写、概括、跨段拼接都会当场露馅。

    `tools/make_eval_cases.py` 与校验脚本都引用这里，不另抄一份。
    """
    return re.sub(r"\s+", "", s or "")


def pronoun_words() -> frozenset[str]:
    """代词表 —— **取自 app.yaml 的 `rules.pronoun_words`**，与 resolve 节点同源。

    ⚠️ 不另抄一份：抄了就会漂移，而漂移之后「生成器认为没问题、图却判澄清」，
       两边对着干还查不出原因。
    """
    import yaml
    cfg_path = repo_path("backend", "app", "config", "app.yaml")
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    return frozenset(data["rules"]["pronoun_words"])


def has_pronoun(question: str, pronouns: frozenset[str] | None = None) -> bool:
    """单轮题里**不许出现代词**（见模块头的实测教训）。"""
    import jieba
    words = pronouns if pronouns is not None else pronoun_words()
    return bool(set(jieba.lcut(question or "")) & words)


def _prompt(chunk: str, title: str, instruction: str) -> str:
    return (
        f"下面是《{title}》里的一段材料：\n\n\"\"\"\n{chunk}\n\"\"\"\n\n"
        f"{instruction}\n\n"
        "严格要求：\n"
        "1. `ground_truth` 必须是上面材料里**连续的一段原文**，一字不改（含标点）。\n"
        "2. 不许用自己的话概括，不许跨出这段材料。\n"
        "3. **问句里不许出现代词**（这个 / 那个 / 该 / 其 / 上述 …）—— "
        "这是**单轮**提问，没有上文，代词会让问题指代不明。"
        "请用「本办法」「这份文件」这类自足的说法，或直接点出文种。\n"
        "4. 只输出 JSON：{\"question\": \"...\", \"ground_truth\": \"...\"}\n\n"
        # ★ few-shot 是决定性的：不给例子时模型爱改写（首轮实测 50 段里只通过 19），
        #   给了例子之后通过率大幅上升 —— 这类「照抄」任务上，一个例子顶十条规则。
        "示例（假设材料里写着「学生应当在考试前向所在学院提出申请，经批准后方可缓考。」）：\n"
        "{\"question\": \"缓考要提前跟谁说、什么时候说？\", "
        "\"ground_truth\": \"学生应当在考试前向所在学院提出申请，经批准后方可缓考。\"}\n"
        "注意答案**逐字**就是材料里的那句话，一个字都没改。"
    )


async def ask_model_for_case(chunk: str, title: str,
                             instruction: str = FACTUAL_INSTRUCTION) -> dict | None:
    """让模型围绕给定片段出一道题。过不了校验就重试，全失败返回 None。

    prompt 与校验由**这里**提供，`tools/make_eval_cases.py` 直接引用 ——
    两边各写一份迟早会漂，而漂移之后「脚本能过、界面过不了」很难查。
    """
    asked = _prompt(chunk, title, instruction)
    for _ in range(MAX_ATTEMPTS):
        try:
            data = await llm.complete_json(
                [{"role": "user", "content": asked}], timeout=45, max_tokens=800)
        except Exception:  # noqa: BLE001 —— 模型抽风就重试，不该拖垮整轮生成
            continue
        if not isinstance(data, dict):
            continue
        question = str(data.get("question", "")).strip()
        answer = str(data.get("ground_truth", "")).strip()
        # ★ 硬校验一：答案必须是这段材料的逐字子串（忽略空白，见 `squeeze`）
        if not (question and answer
                and squeeze(answer) in squeeze(chunk)
                and len(squeeze(answer)) >= 10):
            continue
        # ★ 硬校验二：单轮题**不许带代词**（见模块头的实测教训）
        if has_pronoun(question):
            continue
        return {"question": question, "ground_truth": answer}
    return None


async def _load_document(conn, document_id: str) -> dict | None:
    row = await conn.fetchrow(
        "SELECT id, title FROM documents WHERE id = $1", document_id)
    return dict(row) if row else None


def pick_evenly(items: list, count: int) -> list:
    """**均匀**挑 count 个 —— 不是随机抽。

    随机抽看着更"公平"，但同一份文档点两次生成会拿到完全不同的片段，
    出题结果不可复现；均匀取既覆盖全篇、又稳定。
    """
    if count >= len(items):
        return list(items)
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


async def generate_cases(set_id: str, document_id: str, count: int,
                         *, timeout: float = TOTAL_TIMEOUT) -> dict:
    """同步生成一批用例并入库（决策 18：**同步等待、有上限、超时给部分结果**）。

    返回形状见 §8.2 —— 前端要拿它显示「要了 5 条，实际生成 4 条」这类提示，
    所以 `created` 必须是**实际入库条数**，且 `cases` 只回必要字段。
    """
    count = max(1, min(MAX_COUNT, int(count or DEFAULT_COUNT)))

    async with db.tx() as conn:
        if not await conn.fetchval("SELECT 1 FROM eval_sets WHERE id = $1", set_id):
            raise sets.SetNotFound(set_id)
        document = await _load_document(conn, document_id)
        if document is None:
            raise sets.SetNotFound(f"文档不存在：{document_id}")

    chunks = [c for c in vector.get_chunks(document_id, limit=500)
              if len((c.get("text") or "").strip()) >= MIN_CHUNK_CHARS]
    if not chunks:
        return _result(count, [], False,
                       "这份文档在向量库里没有可用的片段（可能还没索引完，"
                       f"或者片段都短于 {MIN_CHUNK_CHARS} 字）")

    picked = pick_evenly(chunks, count)
    title = document["title"] or ""

    # ---- 并发生成，总超时到点就收 --------------------------------
    semaphore = asyncio.Semaphore(CONCURRENCY)
    done: dict[int, dict] = {}

    async def _one(idx: int, chunk: dict) -> None:
        async with semaphore:
            got = await ask_model_for_case(chunk["text"], title)
        if got:
            done[idx] = {"chunk": chunk, "case": got}

    tasks = [asyncio.create_task(_one(i, c)) for i, c in enumerate(picked)]
    _, pending = await asyncio.wait(tasks, timeout=timeout)
    timed_out = bool(pending)
    for task in pending:
        task.cancel()
    if pending:
        # 等取消真正生效，免得「已取消的任务」在事件循环关闭时报警告
        await asyncio.gather(*pending, return_exceptions=True)

    accepted = [done[i] for i in sorted(done)]

    # ---- 入库 ----------------------------------------------------
    created: list[dict] = []
    if accepted:
        async with db.tx() as conn:
            for item in accepted:
                created.append(await _insert_case(conn, set_id, document_id,
                                                  document, item))

    reasons = []
    failed = len(picked) - len(accepted)
    if failed > 0:
        reasons.append(f"{failed} 条没通过校验"
                       "（标准答案不是原文逐字子串，或问句里带了代词）")
    if timed_out:
        reasons.append(f"生成超时（>{timeout:.0f} 秒），先返回已生成的；"
                       "剩下的可以再点一次")
    return _result(count, created, timed_out, "；".join(reasons))


async def _insert_case(conn, set_id: str, document_id: str, document: dict,
                       item: dict) -> dict:
    """按 §8.1 的字段清单写一条生成出来的用例 —— **一个都不能漏**。"""
    chunk = item["chunk"]
    meta = chunk.get("metadata") or {}
    case = item["case"]
    case_id = uuid.uuid4().hex
    page = meta.get("page")
    await conn.execute(
        """INSERT INTO eval_cases
             (id, set_id, question, ground_truth, case_type, suite,
              expected_doc_ids, expected_chunk_ids, turns, visible_roles,
              expected_route, should_clarify,
              source, in_eval, note,
              source_document_id, source_chunk_id, source_page, source_snippet)
           VALUES ($1,$2,$3,$4,'factual','full',
                   $5,NULL,NULL,NULL,
                   'knowledge',0,
                   'generated',TRUE,NULL,
                   $6,$7,$8,$9)""",
        case_id, set_id, case["question"], case["ground_truth"],
        # ⚠️ `expected_doc_ids` **必须写**：不写这道题在轮次汇总里的
        #    `recall_at_k` / `mrr` 恒为空（§7.6）。出题就是从这份文档出的，
        #    期望文档就是它。
        #    ⚠️ 传 list 原样，**不要 `json.dumps`** —— 连接上已注册 jsonb 编解码器，
        #       再 dumps 一次会把数组存成 JSON 字符串（与 seed 那条路径不一致）。
        [document_id],
        document_id, chunk.get("chunk_id"), page, chunk.get("text"))
    return {"id": case_id, "question": case["question"],
            "ground_truth": case["ground_truth"],
            "source_page": page, "source_chunk_id": chunk.get("chunk_id")}


def _result(requested: int, created: list[dict], timed_out: bool, reason: str) -> dict:
    """§8.2 的响应形状。**条数是目标不是保证**，如实报。"""
    return {
        "requested": requested,
        "created": len(created),
        "failed": requested - len(created),
        "reason": reason,
        "timeout": timed_out,
        # 前端生成完要刷新列表，不需要整行
        "cases": [{"id": c["id"], "question": c["question"],
                   "ground_truth": c["ground_truth"]} for c in created],
    }
