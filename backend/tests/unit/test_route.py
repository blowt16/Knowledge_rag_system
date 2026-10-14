"""节点 2 route —— 三分类与两层收严（§7.2 M2-2 的测试点）。

两条最容易写错的规则（都会造成**直接答非所问**，且规则层没有 LLM 复核机会）：

1. **规则层只吃「明确的」** —— 不判 `clarify`、不做「短 → 闲聊」推断。
   按字面实现「短句判定」的话，「挂科了怎么办」会被判成闲聊，
   而 `chat` 分支明确**不检索、不引用**。
2. **`last_route` 只有 `knowledge` 参与粘性** —— 否则
   「你好」→「那这个要多久啊」被粘成 chat，同样不检索不引用。

⚠️ 兜底到 `knowledge` 时 `route_source` 必须记 **`llm`**（不是 `rule`）：
   它确实走过 LLM 调用、只是没拿到可用结果。记错会污染「规则命中率」的口径 ——
   那个指标的全部意义就是「规则层省下了多少次 LLM 调用」。
"""

from __future__ import annotations

import pytest

from app.core import llm
from app.core.prompts import load_prompt
from app.graph.nodes import route as RT
from app.graph.state import Message, new_state

# 够长（≥8）且不含制度名词/文号 → 规则层判不了、也不触发澄清，才会落到 LLM
VAGUE = "这个东西到底怎么弄才行"

HISTORY = [Message(role="user", content="转专业的申请条件是什么？"),
           Message(role="assistant", content="（答）")]


@pytest.fixture
def no_llm(monkeypatch):
    async def boom(*a, **kw):
        raise AssertionError("规则层命中时不该调用 LLM")
    monkeypatch.setattr(llm, "complete_json", boom)


# ============================================================
# 规则层
# ============================================================

def test_rule_layer_only_classifies_the_obvious():
    assert RT.rule_classify("你好") == "chat"
    assert RT.rule_classify("桂电教〔2025〕28号") == "knowledge"
    assert RT.rule_classify("你好，我想问下缓考") == "knowledge"   # 优先级
    assert RT.rule_classify("缓考") == "knowledge"
    # 规则层不判 clarify —— 意图模糊正是规则判不了的
    assert RT.rule_classify("这个东西怎么弄") is None
    # 不做「短 → 闲聊」推断
    assert RT.rule_classify("挂科了怎么办") == "knowledge"


# ============================================================
# 澄清触发（仅两种）
# ============================================================

def test_clarify_trigger_short_dangling_reference():
    """短句 + 指代无先行词 → 澄清（触发条件：**短且无法定位主题**）。

    ⚠️ 这条原来叫 trigger_one_unresolved_reference，判据是 `resolve_unresolved`
       单独一个标志。2026-10-14 E2E 实测把它改了：那个标志**长句也命中**，
       而长句恰恰不该反问（见本文件末尾「实测缺口（2026-10-14）」一节）。
       现在它与触发条件二合并成同一份判据 —— 短、且没有可定位的主题。
       本用例的输入**行为不变**（仍然澄清），只是依据换了。
    """
    s = new_state(query="那这个要多久啊", resolved_query="那这个要多久啊",
                  resolve_unresolved=True, history=HISTORY)
    assert RT._should_clarify(s) is True


def test_clarify_trigger_two_short_and_no_topic():
    s = new_state(query="怎么办", resolved_query="怎么办", history=HISTORY)
    assert RT._should_clarify(s) is True


def test_no_clarify_when_topic_is_locatable():
    """**误报是体验最差的失败模式** —— 能定位主题就绝不反问。"""
    for q in ["缓考", "转专业", "桂电教〔2025〕28号",
              "我想问一下本科生学业预警以及学业退学实施细则里关于退学的具体规定"]:
        s = new_state(query=q, resolved_query=q, history=HISTORY)
        assert RT._should_clarify(s) is False, f"{q} 不该触发澄清"


# ============================================================
# 节点行为（mock LLM）
# ============================================================

async def test_greeting_is_chat_without_llm(no_llm):
    out = await RT.route_node(new_state(query="你好", resolved_query="你好"))
    assert out["route"] == "chat"
    assert out["route_source"] == "rule"


async def test_priority_knowledge_over_chat(no_llm):
    """§7.2 M2-2 ② 原题：命中闸门词表，但整条消息**有实际诉求**。"""
    q = "你好，我想问下缓考"
    out = await RT.route_node(new_state(query=q, resolved_query=q))
    assert out["route"] == "knowledge"
    assert out["route_source"] == "rule"


async def test_doc_number_is_knowledge_without_llm(no_llm):
    q = "桂电教〔2025〕28号"
    out = await RT.route_node(new_state(query=q, resolved_query=q))
    assert out["route"] == "knowledge"


async def test_last_route_chat_is_not_sticky(no_llm):
    """上轮 chat + 本轮指代无先行词 → **不得粘成 chat**（粘了就答非所问）。"""
    q = "那这个要多久啊"
    out = await RT.route_node(new_state(query=q, resolved_query=q,
                                        last_route="chat", history=HISTORY))
    assert out["route"] != "chat"
    assert out["route"] == "clarify"   # 无先行词 → 澄清


async def test_last_route_chat_but_resolved_to_a_real_question():
    """同样上轮 chat，但本轮已被消解成实义问题 → knowledge。"""
    async def fake(messages, **kw):
        return {"route": "knowledge"}
    monkey = pytest.MonkeyPatch()
    monkey.setattr(llm, "complete_json", fake)
    try:
        out = await RT.route_node(new_state(query="那这个要多久啊",
                                            resolved_query="缓考申请要提前多久",
                                            last_route="chat", history=HISTORY))
    finally:
        monkey.undo()
    assert out["route"] == "knowledge"


async def test_llm_failure_falls_back_to_knowledge_with_source_llm(monkeypatch):
    async def boom(messages, **kw):
        raise llm.LLMTimeout("boom")
    monkeypatch.setattr(llm, "complete_json", boom)

    out = await RT.route_node(new_state(query=VAGUE, resolved_query=VAGUE,
                                        history=HISTORY))
    assert out["route"] == "knowledge"
    assert out["route_source"] == "llm", "兜底必须记 llm，否则规则命中率口径被污染"


async def test_illegal_llm_value_also_falls_back(monkeypatch):
    async def weird(messages, **kw):
        return {"route": "banana"}
    monkeypatch.setattr(llm, "complete_json", weird)

    out = await RT.route_node(new_state(query=VAGUE, resolved_query=VAGUE,
                                        history=HISTORY))
    assert out["route"] == "knowledge"
    assert out["route_source"] == "llm"


async def test_llm_may_choose_chat_or_clarify(monkeypatch):
    async def fake(messages, **kw):
        return {"route": "chat"}
    monkeypatch.setattr(llm, "complete_json", fake)

    out = await RT.route_node(new_state(query=VAGUE, resolved_query=VAGUE,
                                        history=HISTORY))
    assert out["route"] == "chat"
    assert out["route_source"] == "llm"


async def test_sticky_last_route_is_only_knowledge(monkeypatch):
    """送进提示词的「上一轮分类」只有 knowledge 是有效值，其余写「（无）」。"""
    seen: list[str] = []

    async def capture(messages, **kw):
        seen.append(messages[0]["content"])
        return {"route": "knowledge"}

    monkeypatch.setattr(llm, "complete_json", capture)

    for last, expect_sticky in [("knowledge", "knowledge"), ("chat", "（无）"),
                                ("clarify", "（无）"), ("", "（无）")]:
        await RT.route_node(new_state(query=VAGUE, resolved_query=VAGUE,
                                      last_route=last, history=HISTORY))

    for prompt, (last, expect_sticky) in zip(seen, [("knowledge", "knowledge"),
                                                   ("chat", "（无）"),
                                                   ("clarify", "（无）"), ("", "（无）")]):
        # ⚠️ 只查「【上一轮分类】」与「【用户问题】」之间那一段。
        #    原先还挂了一句 `or expect_sticky in prompt` —— 那让 knowledge 那条**恒真**
        #    （提示词正文里本来就枚举了 knowledge 这个词），等于没测。评审 M3。
        section = prompt.split("【上一轮分类】")[1].split("【用户问题】")[0]
        assert expect_sticky in section, f"last_route={last!r} 的粘性写错了：{section!r}"


# ============================================================
# M2 首跑实测发现的缺口（2026-10-05）
# ============================================================

async def test_symbol_only_input_is_not_clarified(no_llm):
    """纯符号输入**不得触发澄清**（§15 #6）。

    ⚠️ 实测现场：题库 mt-13 的「？？？」原来被判成 clarify ——
       短、又没有制度名词，正好踩中澄清触发条件二。用户敲了三个问号，
       系统回一句「你想问的是哪一项？」，这是纯噪声。
       修法是把「没有实义字符」并入闸门判据（与 resolve 共用同一份判据）。
    """
    for q in ["？？？", "。。。", "—— !!!"]:
        out = await RT.route_node(new_state(query=q, resolved_query=q))
        assert out["route"] != "clarify", f"{q} 触发了无意义澄清"
        assert out["route"] == "chat"


async def test_llm_fallback_records_degradation(monkeypatch):
    """兜底必须在 trace 里留痕 —— 评测器的护栏靠它拦「指标失真的那一轮」。

    ⚠️ 评审发现（I2）：护栏承诺「有降级就不写评测文件」，但 route/clarify/rewrite
       三个节点兜底时 `degraded` 恒为 None —— 于是 LLM 静默兜底的那一轮，
       指标照样被写进验收文档，而护栏打印的那句话成了空话。
    """
    async def boom(messages, **kw):
        raise llm.LLMTimeout("boom")
    monkeypatch.setattr(llm, "complete_json", boom)

    out = await RT.route_node(new_state(query=VAGUE, resolved_query=VAGUE, history=HISTORY))
    assert out["route"] == "knowledge"
    assert out["trace"][0]["degraded"], "兜底了却没留痕 —— 评测护栏拦不住"


async def test_llm_garbage_also_records_degradation(monkeypatch):
    async def weird(messages, **kw):
        return {"route": "banana"}
    monkeypatch.setattr(llm, "complete_json", weird)

    out = await RT.route_node(new_state(query=VAGUE, resolved_query=VAGUE, history=HISTORY))
    assert out["trace"][0]["degraded"]


async def test_rule_hit_has_no_degradation(no_llm):
    """反向锁：规则层命中是**正常路径**，不能标成降级（否则护栏永远在拦）。"""
    out = await RT.route_node(new_state(query="缓考", resolved_query="缓考"))
    assert out["trace"][0]["degraded"] is None


# ============================================================
# 实测缺口（2026-10-12）：问「系统自身」被误判 —— 两层各一处，都要堵
# ============================================================

def test_no_clarify_when_asking_about_the_system_itself():
    """「你可以干什么」这类**问系统自身**的消息不得触发澄清。

    ⚠️ 实测现场（qa_logs 08:34:23，route_source=rule）：6 个字、不含制度名词
       → 正好踩中澄清触发条件二，被判「意图模糊」。**可它一点都不模糊。**

    ⚠️ 更要命的是位置：`route_node` 的顺序是**闸门 → 澄清 → 规则层 → LLM**，
       澄清判真就**直接 return**，LLM 连复核的机会都没有（`route.py` 顶部注释
       把「规则层没有 LLM 复核机会」称为全文档最危险的一条 —— 那次删掉的是
       「短句 → 闲聊」，**同一个毛病在澄清这条判据上一直还在**）。
       用户问「你能干什么」，系统回「你想问的是哪一项？」，是纯噪声。
    """
    for q in ["你可以干什么", "你能做什么", "你会什么", "你是谁", "怎么用"]:
        s = new_state(query=q, resolved_query=q, history=HISTORY)
        assert RT._should_clarify(s) is False, f"{q} 不该触发澄清"

    # 反向锁：与系统无关的短问句**仍然**要澄清 —— 边界不能放宽，
    # 放宽了就会把「怎么办」这类真·模糊问句放进检索，答非所问。
    for q in ["怎么办", "还有吗", "那个呢"]:
        s = new_state(query=q, resolved_query=q, history=HISTORY)
        assert RT._should_clarify(s) is True, f"{q} 该澄清却放过了"


def test_no_clarify_for_other_phrasings_of_system_questions():
    """★ 按**说法**枚举词表必然漏 —— 换一种说法就漏一次（用户实测第二次就漏了）。

    ⚠️ 实测现场（qa_logs 2026-10-12 09:28:03，route_source=rule）：
       问「你怎么使用」被澄清。它既不含「怎么用」也不含「如何使用」
       （「怎么」和「使用」中间隔着一个「使」），5 个字 → 照样中招。

    ⚠️ 所以修法**不是再加一条短语** —— 那只会等下一次换说法。
       换成换判据：**消息的主语是系统自身（你 / 您 / 系统 / 助手 / 平台）
       就算「能定位主题」**。主题就是系统本身，不模糊。
    """
    for q in ["你怎么使用", "你咋用", "怎么用你", "这个系统怎么用", "您怎么操作"]:
        s = new_state(query=q, resolved_query=q, history=HISTORY)
        assert RT._should_clarify(s) is False, f"{q} 不该触发澄清"

    # 反向锁照旧：既不是系统自身、也不是用法短语的短问句**仍然**要澄清
    for q in ["怎么办", "还有吗", "那个呢"]:
        s = new_state(query=q, resolved_query=q, history=HISTORY)
        assert RT._should_clarify(s) is True, f"{q} 该澄清却放过了"


def test_no_clarify_for_subjectless_usage_phrasings():
    """没有主语的用法问法也要认（「怎么操作」「咋用」）。

    ⚠️ 这是同一类漏判的**另一半**：主语判据管住了「你怎么使用」，
       管不住「怎么操作」——它没有主语。实测列出来的漏网说法：
       怎么操作 / 咋用 / 这咋用 / 怎么问 / 这个怎么玩。

    ⚠️ 所以判据再换一次，换成**「疑问词 + 用法动词」的模式**，而不是继续
       枚举整句：整句说法的组合是开集，动词却是封闭的一小撮。
    """
    for q in ["怎么操作", "咋用", "这咋用", "怎么问", "怎么使用"]:
        s = new_state(query=q, resolved_query=q, history=HISTORY)
        assert RT._should_clarify(s) is False, f"{q} 不该触发澄清"

    # 反向锁：疑问词 + **非**用法动词，仍然该澄清 —— 模式不能放太宽。
    # 「怎么弄」「咋办」是典型的真·模糊，放过去就会拿去检索、答非所问。
    for q in ["怎么办", "怎么弄", "咋办"]:
        s = new_state(query=q, resolved_query=q, history=HISTORY)
        assert RT._should_clarify(s) is True, f"{q} 该澄清却放过了"


async def test_capability_question_reaches_llm_and_is_chat(monkeypatch):
    """★ 能力问询必须**走到 LLM**（由它定 chat），而不是在规则层被拦成 clarify。

    ⚠️ 这条是上面那条的端到端版本，也是本次 bug 的主回归锁：修复前
       `_should_clarify` 为真 → 直接 return clarify 且 `route_source=rule`，
       LLM 根本不被调用。
    """
    async def fake(messages, **kw):
        return {"route": "chat"}

    monkeypatch.setattr(llm, "complete_json", fake)

    out = await RT.route_node(
        new_state(query="你可以干什么", resolved_query="你可以干什么"))
    assert out["route"] == "chat"
    assert out["route_source"] == "llm", "能力问询该由 LLM 判定，不是规则层直接定"


def test_route_prompt_chat_definition_covers_capability_inquiry():
    """`route` 提示词里 chat 的定义必须覆盖「问系统自身能力/用法」。

    ⚠️ 设计文档里这一类叫「闲聊/**能力问询**」（`intent-recognition-plan.md:422`，
       v2 明写 NO_RAG 含「询问系统能力」），而实现只写了「闲聊」——
       于是 LLM 面对「你的功能范围」时：不是寒暄 → 不是 chat；字面沾
       「校园规章制度」→ 落到 knowledge（qa_logs 08:34:43，route_source=llm）。
    """
    text = load_prompt("route").template
    # 取**整条要点**（从 `- chat` 到下一条 `- xxx` 之前）—— 定义会换行，
    # 只取第一行会把换行后的「有哪些功能」漏掉（写窄了这条测试就成了摆设）。
    bullet = next((b for b in text.split("\n- ") if b.startswith("chat")), "")
    assert bullet, "route 提示词里找不到 chat 的定义"
    assert ("能力" in bullet) or ("功能" in bullet), (
        f"chat 的定义没覆盖能力问询 —— LLM 只能把「你的功能范围」判成 knowledge：{bullet!r}")


def test_chat_prompt_introduces_features_and_guides_asking():
    """`chat` 提示词要**介绍功能**并**引导用户怎么提问**（负责人 2026-10-12 定的口径）。

    只把 chat 当寒暄回应的话，用户问「你能干什么」得不到任何指路，
    等于问了白问 —— 而这条路径的全部价值就是引导。
    """
    text = load_prompt("chat").template
    assert ("功能" in text) or ("能力" in text), "chat 提示词没写「介绍功能/能力」"
    assert "提问" in text, "chat 提示词没写「引导用户怎么提问」"


# ============================================================
# 实测缺口（2026-10-14）：超范围问题被「消解不了」硬拦成澄清
# ============================================================

def test_no_clarify_for_long_query_with_unresolved_pronoun():
    """★ 主回归锁：**长句**带无先行词的代词，不得触发澄清。

    ⚠️ 实测现场（E2E 多轮实测第 13 轮，`route_source=rule`）：
       用户问「我之前跟你说的那个数学建模比赛，报名截止是几号来着？」——
       30 个字，远超 8 字下限，可 `_should_clarify` 却为真、直接判澄清，
       **路由 LLM 连被调用的机会都没有**。用户得到的是「你想问什么？」，
       而他这句话里明明写清楚了自己想问什么。

    ⚠️ 成因是判据本身：`resolve_unresolved = resolved == query and has_pronoun(query)`
       把「消解器没能改写」当成了「指代无法消解」。可消解提示词明确要求
       **「已经自包含就原样返回」**——于是「那个数学建模比赛」这种
       **代词作定语、主题就在句中**的正常问句，被判成了消解失败。
       同一个输出（`resolved == query`）在这里代表了**两件相反的事**。

    ⚠️ 更要命的是位置：长度判据（≥8 放行）在它后面，所以这个硬拦
       **只对长句生效** —— 恰恰是它最不该生效的地方。短句本来就会被
       长度判据兜住，标志对它们毫无增量。
    """
    for q in ["我之前跟你说的那个数学建模比赛，报名截止是几号来着？我给忘了",
              "那我之前跟你说的社团招新，是下周几来着？",
              "你之前跟我说的那个缓考申请，截止日期是哪天来着？",
              "我室友是哪个专业的来着？我又忘了"]:
        s = new_state(query=q, resolved_query=q, resolve_unresolved=True, history=HISTORY)
        assert RT._should_clarify(s) is False, (
            f"长句被「消解不了」硬拦成澄清了（{len(q)} 字）：{q}")


def test_no_clarify_when_pronoun_is_a_determiner():
    """代词作**定语**、主题就在句子里时不算「定位不到主题」。

    「那个缓考申请」的「那个」是定语不是指代 —— 去掉它，「缓考申请」照样
    是个自足的查询。这类问句该进检索，不该被反问。
    """
    for q in ["那个缓考申请要提前几天", "这个转专业的条件是什么",
              "那个学业预警的绩点标准是多少"]:
        s = new_state(query=q, resolved_query=q, resolve_unresolved=True, history=HISTORY)
        assert RT._should_clarify(s) is False, f"{q} 主题就在句子里，不该反问"


async def test_unresolved_pronoun_on_long_query_reaches_llm(monkeypatch):
    """★ 端到端版本：长句 + `resolve_unresolved` 必须**走到 LLM**，不得在规则层定死。

    与 `test_capability_question_reaches_llm_and_is_chat` 同一类回归锁 ——
    修复前 `_should_clarify` 为真 → 直接 return clarify 且 `route_source=rule`，
    LLM 根本不被调用，所以它无从把超范围问题判成 chat 去引导用户。
    """
    async def fake(messages, **kw):
        return {"route": "chat"}

    monkeypatch.setattr(llm, "complete_json", fake)

    q = "我之前跟你说的那个数学建模比赛，报名截止是几号来着？我给忘了"
    out = await RT.route_node(new_state(query=q, resolved_query=q,
                                        resolve_unresolved=True, history=HISTORY))
    assert out["route_source"] == "llm", (
        "长句被规则层硬拦了 —— LLM 没机会把它判成 chat 并引导用户")
    assert out["route"] == "chat"


def test_short_dangling_reference_still_clarifies():
    """反向锁：**短**句指代无先行词**仍然**要澄清 —— 边界不能整条放宽。

    放宽过头就会把「那这个要多久啊」放进检索，那是答非所问。
    真正模糊的是「说不出想问什么」，这类才是澄清该管的事。
    """
    for q in ["那这个要多久啊", "那个呢", "怎么办", "还有吗"]:
        s = new_state(query=q, resolved_query=q, resolve_unresolved=True, history=HISTORY)
        assert RT._should_clarify(s) is True, f"{q} 该澄清却放过了"
