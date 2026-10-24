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
from pathlib import Path

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
#: 多轮一次要生成整段对话，给得更宽；超时同样返回已生成部分。
MULTI_TIMEOUT = 180.0

#: 并发几路问模型。3 路时 5 条约 20~30 秒。
CONCURRENCY = 3
MULTI_CONCURRENCY = 3

#: 多轮一次输出整段 JSON，token 预算比单轮大
MULTI_MAX_TOKENS = 2000

#: 太短的片段出不了像样的题（与旧脚本同口径）
MIN_CHUNK_CHARS = 120

#: 单段/单窗口最多问几次（首次 + 重试 2 次）
MAX_ATTEMPTS = 3

#: 标准答案切分：按句号/分号/感叹/问号/换行切，**不按逗号切**
_CLAUSE_RE = re.compile(r"[。；！？\n]+")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_ALPHA_RE = re.compile(r"[A-Za-z]{2,}")

FACTUAL_INSTRUCTION = ("出一道路人式的事实题：问某项规定/条件/时限是**什么**。"
                       "问法要像一个学生随口问的，不要照抄原文句式。")


class DocumentNotIndexed(LookupError):
    """这份文档在向量库里没有可用的片段 —— 接口层译成 409/400。"""


def squeeze(s: str) -> str:
    """去掉**所有空白 + 表格序列化分隔符**再比。用于「逐字」校验。

    ⚠️ 空白必须去掉：规范化正文里有 PDF 提取留下的硬换行与空行
    （实测：`…提出申请并经\\n\\n学院审核同意后送达；`），模型复述时自然写成
    一行 —— 用带空白的原串去比会判成「不是原文」，而**它确实是原文**。

    ⚠️ `｜` 与 `：` 也要去掉（2026-10-15 放宽）：表格改成键值序列化后
    （`参考学分：2分/次`），标准答案与正文之间的分隔符不属于「字」——
    不去掉的话表格题的标准答案**永远校验不过**（题面抄的是一句话，
    正文里却处处隔着 `｜`／`：`）。

    代价如实写明：**校验强度降低了**。去掉冒号后，原本被 `：` 隔开的两段
    文字（`A：B` 与 `AB`）会被判成同一串 —— 含 `：` 的正文题会校验得更松。
    这是负责人 2026-10-15 明确接受的取舍，不是疏忽。

    `tools/make_eval_cases.py` 与 `tests/integration/test_eval_cases.py` 都 import
    这里，不另抄一份 —— 抄一份就会漂移，而漂移之后「生成期放行、校验期报
    不是原文」是**一句看不懂的假失败**，最难查（实测踩过）。

    ⚠️ 唯一的例外是 `app/eval/sets.py::locate_highlight()`：它要把命中区间映射回
       **原文偏移**，只能自己逐字符扫一遍，用的是「只去空白」口径。于是界面高亮
       比这里更严（宁可不高亮，也不高亮错位置）—— 这是有意的，别去「对齐」它。
    """
    return re.sub(r"[\s｜：]+", "", s or "")


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


# ---- 标准答案：逐句原文拼接（单轮/多轮共用）----------------------------

def split_clauses(answer: str) -> list[str]:
    """按 `。；！？\\n` 切句，**不按逗号切**（避免“可以申请”这类过短泛化片段）。"""
    return [c.strip() for c in _CLAUSE_RE.split(answer or "") if c.strip()]


def _substantive(clause: str) -> bool:
    """实质分句：归一化后 ≥8 字，或含数字，或含长度 ≥2 的英文 token。"""
    squeezed = squeeze(clause)
    return (len(squeezed) >= 8 or bool(_NUMBER_RE.search(squeezed))
            or bool(_ALPHA_RE.search(squeezed)))


def validate_ground_truth(answer: str, window_text: str) -> tuple[bool, str]:
    """标准答案是否由 1~3 个原文分句组成（方案 A）。

    返回 `(ok, reason)`。允许分句来自窗口不同位置，不要求连续；
    每个分句（包括“此外”这类连接词）都必须在窗口里，否则拒绝 ——
    严格版宁可拒绝，也不接受改写或原文没有的连接词。
    """
    answer = (answer or "").strip()
    if not answer:
        return False, "标准答案为空"
    window = squeeze(window_text)
    clauses = split_clauses(answer)
    substantive = [c for c in clauses if _substantive(c)]
    if not substantive:
        return False, "没有实质分句"
    if len(substantive) > 3:
        return False, "实质分句超过 3 句"
    for clause in clauses:
        if squeeze(clause) not in window:
            return False, f"分句不是窗口原文：{clause[:30]}"
    for number in _NUMBER_RE.findall(answer):
        if number not in window:
            return False, f"数字不在原文：{number}"
    total = len(squeeze(answer)) or 1
    covered = sum(len(squeeze(c)) for c in substantive)
    if covered / total < 0.8:
        return False, "实质分句覆盖率不足 0.8"
    return True, ""


def _prompt(chunk: str, title: str, instruction: str) -> str:
    return (
        f"下面是《{title}》里的一段材料：\n\n\"\"\"\n{chunk}\n\"\"\"\n\n"
        f"{instruction}\n\n"
        "严格要求：\n"
        "1. `ground_truth` 由 1~3 个原文分句组成；多个分句直接用“；”或“。”"
        "按原文拼接，不要改写、不要概括、不要添加原文没有的连接词。\n"
        "2. 每个实质分句（≥8 字、含数字或含英文词）都必须是上面材料里的逐字原文，"
        "不要求连续；所有数字都必须出现在材料里。\n"
        "3. **问句里不许出现代词**（这个 / 那个 / 该 / 其 / 上述 …）—— "
        "这是**单轮**提问，没有上文，代词会让问题指代不明。"
        "请用「本办法」「这份文件」这类自足的说法，或直接点出文种。\n"
        "4. 只输出 JSON：{\"question\": \"...\", \"ground_truth\": \"...\"}\n\n"
        # ★ few-shot 是决定性的：不给例子时模型爱改写（首轮实测 50 段里只通过 19），
        #   给了例子之后通过率大幅上升 —— 这类「照抄」任务上，一个例子顶十条规则。
        "示例（假设材料里写着「学生应当在考试前向所在学院提出申请。"
        "经批准后方可缓考。」）：\n"
        "{\"question\": \"缓考要提前跟谁说、什么时候说？\", "
        "\"ground_truth\": \"学生应当在考试前向所在学院提出申请。经批准后方可缓考。\"}\n"
        "注意答案由两句原文组成，每一句都能在材料里逐字找到。"
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
        # ★ 硬校验一：逐句原文拼接（单轮/多轮共用）
        if not question:
            continue
        if not validate_ground_truth(answer, chunk)[0]:
            continue
        # ★ 硬校验二：单轮题**不许带代词**（见模块头的实测教训）
        if has_pronoun(question):
            continue
        return {"question": question, "ground_truth": answer}
    return None


async def _load_document(conn, document_id: str) -> dict | None:
    row = await conn.fetchrow(
        "SELECT id, title, normalized_text_path FROM documents WHERE id = $1",
        document_id)
    return dict(row) if row else None


def _read_normalized(path: str | None) -> str | None:
    """读规范化正文，供窗口按 `char_start/char_end` 合并。读不到就退回拼接去重。"""
    if not path:
        return None
    p = Path(path)
    if not p.suffix:
        p = p.with_suffix(".txt")
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _merge_by_overlap(texts: list[str]) -> str:
    """没有 char 偏移时的退化路径：拼接并去掉最长前后缀重叠。"""
    out = ""
    for text in texts:
        text = text or ""
        if not out:
            out = text
            continue
        max_k = min(len(out), len(text))
        for k in range(max_k, 0, -1):
            if out.endswith(text[:k]):
                out += text[k:]
                break
        else:
            out += text
    return out


def build_windows(chunks: list[dict], window: int, count: int,
                  normalized_text: str | None = None) -> list[dict]:
    """相邻 `window` 个 chunk 组成候选窗口。

    - 窗口数够 `count` → 均匀取起点；不够 → 允许重叠，仍按 `count` 取起点
      （不减少生成条数；真正重复的窗口由模型生成不同问法，第一版不去重）。
    - 有 `char_start/char_end` + 规范化正文时按偏移合并，避免 chunk 重叠重复；
      否则退化为拼接 + 最长前后缀去重。
    """
    chunks = sorted(
        [c for c in chunks if (c.get("text") or "").strip()],
        key=lambda c: (c.get("metadata") or {}).get("chunk_index", 0),
    )
    window = max(2, min(3, int(window or 3)))
    count = max(1, int(count or 1))
    if not chunks:
        return []
    if len(chunks) <= window:
        starts = [0]
    else:
        max_start = len(chunks) - window
        starts = [round(i * max_start / max(1, count - 1)) for i in range(count)]

    out: list[dict] = []
    for start in starts:
        picked = chunks[start:start + window]
        metas = [c.get("metadata") or {} for c in picked]
        text = ""
        if (normalized_text and all(
                isinstance(m.get("char_start"), int)
                and isinstance(m.get("char_end"), int)
                and 0 <= m["char_start"] < m["char_end"] <= len(normalized_text)
                for m in metas)):
            lo = min(m["char_start"] for m in metas)
            hi = max(m["char_end"] for m in metas)
            text = normalized_text[lo:hi]
        if not text:
            text = _merge_by_overlap([c.get("text") or "" for c in picked])
        out.append({"chunks": picked, "text": text.strip()})
    return out


def _multi_prompt(window: str, title: str, target_turns: int,
                  instruction: str | None) -> str:
    extra = (
        f"\n【管理员额外要求】\n{instruction}\n"
        "额外要求只影响选题与问法；与下面的硬性要求冲突时，一律以硬性要求为准。\n"
        if instruction else ""
    )
    return (
        f"下面是《{title}》里连续的一段材料：\n\n\"\"\"\n{window}\n\"\"\"\n\n"
        f"{extra}"
        f"请围绕这段材料生成 {target_turns} 轮连续追问（2~4 轮都接受）。\n"
        "严格要求：\n"
        "1. 只输出 JSON：{\"turns\":[{\"question\":\"...\",\"ground_truth\":\"...\"}]}\n"
        "2. 第一轮必须自足，问句里**不许出现代词**。\n"
        "3. 后续轮必须使用代词或省略（那 / 它 / 这个 / 该 / 其 / 多久……）。\n"
        "4. 每轮 ground_truth 由 1~3 个原文分句组成；多个分句按原文用“；”或“。”拼接，"
        "不要改写、概括或添加原文没有的连接词；每个实质分句都必须是窗口材料里的逐字原文。\n"
        "5. 各轮答案不能重复，至少覆盖 2 个不同事实。\n"
        "6. 不许跨出窗口材料，不许用常识补全。\n"
        "7. 轮数 2~4，默认 3。\n\n"
        "示例：\n"
        "{\"turns\":[\n"
        " {\"question\":\"退役复学可以申请什么？\","
        "\"ground_truth\":\"退役复学学生可以申请免修课程。\"},\n"
        " {\"question\":\"那成绩怎么记？\","
        "\"ground_truth\":\"免修成绩按85分记录。\"}\n"
        "]}\n"
    )


def validate_multi_dialogue(data: dict, window_text: str, title: str,
                            target_turns: int) -> dict | None:
    """多轮结构校验：2~4 轮、首轮无代词、每个答案逐句原文、答案不重复。

    ⚠️ “后续轮必须用代词/省略”**只写进 Prompt，不做硬拒绝**：
       “办理流程要多久？”这类合法省略不含代词，硬拦会误伤。
    """
    turns = data.get("turns") if isinstance(data, dict) else None
    if not isinstance(turns, list) or not (2 <= len(turns) <= 4):
        return None
    questions: list[str] = []
    answers: list[str] = []
    cleaned: list[dict] = []
    for i, raw in enumerate(turns, start=1):
        if not isinstance(raw, dict):
            return None
        question = str(raw.get("question") or "").strip()
        ground_truth = str(raw.get("ground_truth") or "").strip()
        if not question or not ground_truth or question in questions:
            return None
        if i == 1 and has_pronoun(question):
            return None
        if not validate_ground_truth(ground_truth, window_text)[0]:
            return None
        squeezed = squeeze(ground_truth)
        if squeezed in answers:
            return None
        questions.append(question)
        answers.append(squeezed)
        cleaned.append({
            "question": question,
            "ground_truth": ground_truth,
            "expected_route": "knowledge",
            "should_clarify": 0,
            "expected_document": title,
        })
    return {"turns": cleaned}


async def ask_model_for_multi(window_text: str, title: str, target_turns: int,
                              instruction: str | None = None) -> dict | None:
    """一次调用生成整段对话；结构/原文校验不过就重试，最多 3 次。"""
    prompt = _multi_prompt(window_text, title, target_turns, instruction)
    for _ in range(MAX_ATTEMPTS):
        try:
            data = await llm.complete_json(
                [{"role": "user", "content": prompt}],
                timeout=90, max_tokens=MULTI_MAX_TOKENS)
        except Exception:  # noqa: BLE001 —— 模型抽风就重试
            continue
        got = validate_multi_dialogue(data, window_text, title, target_turns)
        if got:
            return got
    return None


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
                         *, mode: str = "single", turns: int = 3,
                         window: int = 3, instruction: str | None = None,
                         timeout: float | None = None) -> dict:
    """同步生成一批用例并入库（决策 18：**同步等待、有上限、超时给部分结果**）。

    - `mode="single"`：每个片段一道单轮题（1~3 个原文分句拼接）。
    - `mode="multi"`：每 `window` 个相邻 chunk 一道整段对话（2~4 轮）。

    返回形状见 §8.2 —— 前端要拿它显示「要了 5 条，实际生成 4 条」这类提示，
    所以 `created` 必须是**实际入库条数**，且 `cases` 只回必要字段。
    """
    count = max(1, min(MAX_COUNT, int(count or DEFAULT_COUNT)))
    mode = "multi" if mode == "multi" else "single"
    turns = max(2, min(4, int(turns or 3)))
    window = max(2, min(3, int(window or 3)))
    instruction = (instruction or "").strip()[:500] or None

    async with db.tx() as conn:
        if not await conn.fetchval("SELECT 1 FROM eval_sets WHERE id = $1", set_id):
            raise sets.SetNotFound(set_id)
        document = await _load_document(conn, document_id)
        if document is None:
            raise sets.SetNotFound(f"文档不存在：{document_id}")

    title = document["title"] or ""
    all_chunks = vector.get_chunks(document_id, limit=500)
    if not all_chunks:
        return _result(count, [], False,
                       "这份文档在向量库里没有可用的片段（可能还没索引完）")

    if mode == "multi":
        normalized = _read_normalized(document.get("normalized_text_path"))
        windows = [
            w for w in build_windows(all_chunks, window, count, normalized)
            if len(squeeze(w["text"])) >= MIN_CHUNK_CHARS
        ]
        if not windows:
            return _result(count, [], False,
                           f"这份文档没有可用的相邻片段（窗口文本短于 "
                           f"{MIN_CHUNK_CHARS} 字）")
        units = windows
        timeout = timeout if timeout is not None else MULTI_TIMEOUT
        semaphore = asyncio.Semaphore(MULTI_CONCURRENCY)
        done: dict[int, dict] = {}

        async def _one_multi(idx: int, win: dict) -> None:
            async with semaphore:
                got = await ask_model_for_multi(win["text"], title, turns, instruction)
            if got:
                done[idx] = {"window": win, "turns": got["turns"],
                             "instruction": instruction}

        tasks = [asyncio.create_task(_one_multi(i, w))
                 for i, w in enumerate(units)]
    else:
        chunks = [c for c in all_chunks
                  if len((c.get("text") or "").strip()) >= MIN_CHUNK_CHARS]
        if not chunks:
            return _result(count, [], False,
                           "这份文档在向量库里没有可用的片段（可能还没索引完，"
                           f"或者片段都短于 {MIN_CHUNK_CHARS} 字）")
        units = pick_evenly(chunks, count)
        timeout = timeout if timeout is not None else TOTAL_TIMEOUT
        semaphore = asyncio.Semaphore(CONCURRENCY)
        done = {}

        async def _one_single(idx: int, chunk: dict) -> None:
            async with semaphore:
                got = await ask_model_for_case(
                    chunk["text"], title, instruction or FACTUAL_INSTRUCTION)
            if got:
                done[idx] = {"chunk": chunk, "case": got, "instruction": instruction}

        tasks = [asyncio.create_task(_one_single(i, c))
                 for i, c in enumerate(units)]

    # ---- 并发生成，总超时到点就收 --------------------------------
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
                if mode == "multi":
                    created.append(await _insert_multi_case(
                        conn, set_id, document_id, document, item))
                else:
                    created.append(await _insert_case(
                        conn, set_id, document_id, document, item))

    reasons = []
    if len(units) < count:
        reasons.append(f"可用片段/窗口只有 {len(units)} 个，少于请求条数 {count}")
    failed = len(units) - len(accepted)
    if failed > 0:
        reasons.append(
            f"{failed} 条没通过校验"
            + ("（轮数 2~4、首轮无代词、每轮答案逐句原文、答案不重复）"
               if mode == "multi"
               else "（标准答案不是逐句原文，或问句里带了代词）"))
    if timed_out:
        reasons.append(f"生成超时（>{timeout:.0f} 秒），先返回已生成的；"
                       "剩下的可以再点一次")
    return _result(count, created, timed_out, "；".join(reasons))


def _note(instruction: str | None) -> str | None:
    return f"生成要求：{instruction[:200]}" if instruction else None


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
                   'generated',TRUE,$6,
                   $7,$8,$9,$10)""",
        case_id, set_id, case["question"], case["ground_truth"],
        # ⚠️ `expected_doc_ids` **必须写**：不写这道题在轮次汇总里的
        #    `recall_at_k` / `mrr` 恒为空（§7.6）。
        #    ⚠️ 传 list 原样，**不要 `json.dumps`** —— 连接上已注册 jsonb 编解码器。
        [document_id],
        _note(item.get("instruction")),
        document_id, chunk.get("chunk_id"), page, chunk.get("text"))
    return {"id": case_id, "question": case["question"],
            "ground_truth": case["ground_truth"], "turn_count": 1,
            "source_page": page, "source_chunk_id": chunk.get("chunk_id")}


async def _insert_multi_case(conn, set_id: str, document_id: str,
                             document: dict, item: dict) -> dict:
    """多轮题：question = turns[0]，ground_truth = NULL，答案只放 turns。"""
    window = item["window"]
    turns = item["turns"]
    first = window["chunks"][0]
    meta = first.get("metadata") or {}
    case_id = uuid.uuid4().hex
    page = meta.get("page")
    await conn.execute(
        """INSERT INTO eval_cases
             (id, set_id, question, ground_truth, case_type, suite,
              expected_doc_ids, expected_chunk_ids, turns, visible_roles,
              expected_route, should_clarify,
              source, in_eval, note,
              source_document_id, source_chunk_id, source_page, source_snippet)
           VALUES ($1,$2,$3,NULL,'multi_turn','full',
                   $4,NULL,$5,NULL,
                   'knowledge',0,
                   'generated',TRUE,$6,
                   $7,$8,$9,$10)""",
        case_id, set_id, turns[0]["question"],
        [document_id], turns,
        _note(item.get("instruction")),
        document_id, first.get("chunk_id"), page, window["text"])
    return {"id": case_id, "question": turns[0]["question"],
            "ground_truth": None, "turn_count": len(turns),
            "source_page": page, "source_chunk_id": first.get("chunk_id")}


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
                   "ground_truth": c["ground_truth"],
                   "turn_count": c.get("turn_count", 1)} for c in created],
    }
