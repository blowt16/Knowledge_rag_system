"""脱敏护栏（【方案 §7 / §12.1】#3）。

**实测确认的问题**：日志那边脱敏做了，但 span 是另一个存储。
Langfuse 默认会记录 input/output —— 后果是「日志干净了，云端却摆着学号姓名」。

本测试用**本地捕获服务器**断言实际发出的字节，不是断言 SDK 的自我声明。
"""
from __future__ import annotations

import json

import pytest

from app.core import observability
from tests.lf_capture import CapturingLangfuse

#: 必须被打码的测试学号（10 位纯数字）
STUDENT_ID = "2023123456"
#: 手机号
PHONE = "13800138000"
#: 手机号的三种常见写法都要被打码（分隔符不能绕过脱敏）
PHONE_FORMS = [PHONE, "138-0013-8000", "138 0013 8000"]
#: 邮箱
EMAIL = "zhangsan@example.edu.cn"
#: 身份证：18 位（含校验位 X）与 15 位遗留格式都要被打码
ID_CARD_FORMS = ["11010519491231002X", "110105491231002"]
#: 8 位数字：**不在任何规则内**，用来钉住 `\d{10}` 的长度边界
SHORT_NUMBER = "87654321"
#: 邮箱（英文域名）
TIGHT_EMAIL = "abc@example.com"
#: ⚠️「中文紧贴标识符」——**这才是用户的自然写法**，历史漏网点。
#:    原因：Python `re` 对 str 默认 Unicode，`\w` **包含中文**，
#:    于是「中文字 + 数字」之间根本没有 `\b`，`\b` 版的规则整条失效。
TIGHT_STUDENT_TEXT = f"我的学号是{STUDENT_ID}"
TIGHT_PHONE_TEXT = f"联系方式{PHONE}"
TIGHT_EMAIL_TEXT = f"邮箱{TIGHT_EMAIL}"
#: 带国码的长数字串（13 位）：手机号规则**不许**从中间啃掉一段
LONG_DIGIT_RUN = "8613800138000"


@pytest.fixture()
def capture(monkeypatch):
    with CapturingLangfuse() as cap:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-dummy")
        monkeypatch.setenv("LANGFUSE_BASE_URL", cap.base_url)
        observability._reset_for_test()
        observability.setup("campus-rag")
        yield cap
        observability.shutdown()


def test_student_id_is_masked_before_export(capture):
    """★ 核心断言：学号不得出现在发出的字节里，打码标记必须出现。"""
    client = observability.get_client()
    assert client is not None

    with client.start_as_current_observation(
        as_type="generation", name="llm-call", model="deepseek-flash",
    ) as gen:
        gen.update(
            input=[{"role": "user", "content": f"学号 {STUDENT_ID} 的缓考申请"}],
            output=f"已受理（学号 {STUDENT_ID}）",
            usage_details={"input": 10, "output": 5},
        )
    client.flush()

    body = capture.body()
    assert body, "本地捕获服务器没收到任何东西 —— 说明数据根本没发出来"
    assert STUDENT_ID not in body, f"学号原样发出去了！收到的内容：\n{body[:500]}"
    assert "[REDACTED:student_id]" in body, "打码标记没出现"


@pytest.mark.parametrize("form", PHONE_FORMS)
def test_phone_is_masked(capture, form):
    """带分隔符的写法也要挡住 —— 加个 `-` 就能绕过脱敏是最典型的漏网方式。"""
    client = observability.get_client()
    with client.start_as_current_observation(as_type="span", name="q") as s:
        s.update(input=f"联系方式 {form}")
    client.flush()

    body = capture.body()
    assert body, "本地捕获服务器没收到任何东西 —— 说明数据根本没发出来"
    assert form not in body, f"手机号原样发出去了：{form}"
    assert "[REDACTED:phone]" in body


@pytest.mark.parametrize("form", ID_CARD_FORMS)
def test_id_card_is_masked(capture, form):
    """18 位与 15 位遗留格式都要挡住 —— 只挡 18 位等于把老证件漏出去。"""
    client = observability.get_client()
    with client.start_as_current_observation(as_type="span", name="q") as s:
        s.update(input=f"证件号 {form}")
    client.flush()

    body = capture.body()
    assert body, "本地捕获服务器没收到任何东西 —— 说明数据根本没发出来"
    assert form not in body, f"身份证原样发出去了：{form}"
    assert "[REDACTED:id_card]" in body


def test_email_is_masked(capture):
    client = observability.get_client()
    with client.start_as_current_observation(as_type="span", name="q") as s:
        s.update(input=f"联系邮箱 {EMAIL}")
    client.flush()

    body = capture.body()
    assert body, "本地捕获服务器没收到任何东西 —— 说明数据根本没发出来"
    assert EMAIL not in body, "邮箱原样发出去了"
    assert "[REDACTED:email]" in body


def test_student_id_masked_when_tight_against_chinese(capture):
    """★ 中文紧贴数字时也必须打码（历史漏网点）。

    `\\b` 在「中文字 + 数字」之间**不存在**（`\\w` 含中文），
    所以 `\\b` 版的规则对「我的学号是2023123456」整条失效 ——
    而这正是用户不带空格的日常写法。brief 的用例全带空格，故一直没暴露。
    """
    client = observability.get_client()
    with client.start_as_current_observation(as_type="span", name="q") as s:
        s.update(input=TIGHT_STUDENT_TEXT)
    client.flush()

    body = capture.body()
    assert body, "本地捕获服务器没收到任何东西 —— 说明数据根本没发出来"
    assert STUDENT_ID not in body, f"紧贴中文的学号原样发出去了：\n{body[:500]}"
    assert "[REDACTED:student_id]" in body


def test_phone_masked_when_tight_against_chinese(capture):
    """同上学号：`联系方式13800138000` 不带空格也要挡住。"""
    client = observability.get_client()
    with client.start_as_current_observation(as_type="span", name="q") as s:
        s.update(input=TIGHT_PHONE_TEXT)
    client.flush()

    body = capture.body()
    assert body, "本地捕获服务器没收到任何东西 —— 说明数据根本没发出来"
    assert PHONE not in body, f"紧贴中文的手机号原样发出去了：\n{body[:500]}"
    assert "[REDACTED:phone]" in body


def test_email_masked_without_eating_chinese_prefix(capture):
    """邮箱：既打码，又**不许把前面的中文一起吞进替换串**。

    `[\\w.+-]+` 里的 `\\w` 含中文，会把「邮箱」也算作邮箱地址的一部分 ——
    打码虽然发生了，但正文被啃掉一块，等于另一种损坏。字符类必须写成显式 ASCII。
    """
    client = observability.get_client()
    with client.start_as_current_observation(as_type="span", name="q") as s:
        s.update(input=TIGHT_EMAIL_TEXT)
    client.flush()

    body = capture.body()
    assert body, "本地捕获服务器没收到任何东西 —— 说明数据根本没发出来"
    assert TIGHT_EMAIL not in body, "邮箱原样发出去了"
    assert "[REDACTED:email]" in body
    assert "邮箱" in body, "邮箱前面的中文被一起吞掉了 —— 字符类写太宽（\\w 含中文）"


def test_longer_digit_run_is_not_partially_masked(capture):
    """负控：带国码的长数字串，不许被手机号规则从**中间**啃掉一段。

    放宽手机号规则（容忍分隔符）时最容易踩的坑：规则匹配到了 11 位数字，
    但那其实是更长数字串的一部分 —— 打码后串就被截断了，比不打码更糟。
    """
    client = observability.get_client()
    with client.start_as_current_observation(as_type="span", name="q") as s:
        s.update(input=f"号码 {LONG_DIGIT_RUN}")
    client.flush()

    body = capture.body()
    assert body, "本地捕获服务器没收到任何东西 —— 说明数据根本没发出来"
    assert LONG_DIGIT_RUN in body, f"长数字串被啃掉了一段：\n{body[:500]}"


def test_non_sensitive_text_survives(capture):
    """正控：脱敏不能把正常正文也抹掉（否则上面两条断言可能只是因为什么都没发）。"""
    client = observability.get_client()
    with client.start_as_current_observation(as_type="span", name="q") as s:
        s.update(input=f"图书馆开放时间是什么时候，通知编号 {SHORT_NUMBER}")
    client.flush()

    body = capture.body()
    assert body, "本地捕获服务器没收到任何东西 —— 说明数据根本没发出来"
    assert "图书馆开放时间" in body, "正常正文被误删了"
    # 钉住学号规则的长度边界：8 位数不在任何规则内。
    # 放宽 `\d{10}` 必须是一个**显式决定**，不能悄悄发生（也防规则被误改宽）。
    assert SHORT_NUMBER in body, "8 位数字被误伤 —— 学号规则的边界被动过了"


# ============================================================
# 转义文本（容器输入）—— ★ 上面那一整批用例集体躲开的那一类
# ============================================================
# SDK 序列化规则（实测 `_client/attributes.py:_serialize`）：
#   · **字符串原样**（`s.update(input="…")`）—— 中文是字面量，`(?<!\d)` 之前没数字；
#   · **容器走 `json.dumps(..., ensure_ascii=True)`**（`input=[{...}]`、`metadata`）
#     —— 中文变成 `\uXXXX`。于是数字规则的前视 `(?<!\d)` 撞上的「前一个字符」
#     是**转义末位的十六进制字符**：是数字就静默卡掉打码。
#     `是`=`\\u662f`（末位 f）侥幸通过，`号`=`\\u53f7`（末位 7）整条规则失效 ——
#     22 个常见前导字里 16 个如此（`logs/acceptance/A7-prefix-demo.txt` 验收实测）。
# ⚠️ 上面那批用例的 `TIGHT_STUDENT_TEXT` 恰好用了 `是`，故一直全绿。

#: 末位十六进制是**数字**的常见前导字 —— 旧实现下 `学号2023123456` 这类写法全漏
ESCAPE_DIGIT_TAIL_PREFIXES = ["号", "的", "学", "我", "要", "需"]
#: 末位是字母的前导字（`是`=`\\u662f`）—— 负控：修完更不能把本来能过的弄坏
ESCAPE_LETTER_TAIL_PREFIX = "是"


@pytest.mark.parametrize("prefix", ESCAPE_DIGIT_TAIL_PREFIXES)
def test_student_id_masked_after_chinese_inside_json_serialized_container(capture, prefix):
    """★★ 核心回归点：容器输入（SDK 转义成 `\\uXXXX`）里，中文紧贴学号也要打码。

    ⚠️ 这条用例修前必红（`prefix` 全是「转义末位是数字」的字）。漏打码是
       **静默**的 —— 不报错、不告警，验收只能靠反查云端收到的字节。
    """
    client = observability.get_client()
    text = f"{prefix}{STUDENT_ID}的学生，申请转专业需要满足哪些条件？"
    with client.start_as_current_observation(
        as_type="generation", name="llm.generate", model="deepseek-flash",
    ) as gen:
        gen.update(input=[{"role": "user", "content": text}])
    client.flush()

    body = capture.body()
    assert body, "本地捕获服务器没收到任何东西 —— 说明数据根本没发出来"
    assert STUDENT_ID not in body, (
        f"容器输入里的学号原样发出去了（前导字「{prefix}」）：\n{body[:500]}"
    )
    assert "[REDACTED:student_id]" in body, f"打码标记没出现（前导字「{prefix}」）"


@pytest.mark.parametrize("prefix", [ESCAPE_LETTER_TAIL_PREFIX])
def test_student_id_still_masked_when_escape_tail_is_a_letter(capture, prefix):
    """负控：本来就能过的那一半（`是`）修完必须照样过。"""
    client = observability.get_client()
    with client.start_as_current_observation(as_type="span", name="q") as s:
        s.update(input=[{"role": "user", "content": f"{prefix}{STUDENT_ID}的学生"}])
    client.flush()

    body = capture.body()
    assert body, "本地捕获服务器没收到任何东西 —— 说明数据根本没发出来"
    assert STUDENT_ID not in body, f"学号原样发出去了：\n{body[:500]}"
    assert "[REDACTED:student_id]" in body


#: 数字规则的边界矩阵（验收逐条要求的位置）。`should_mask` 按**解码后的正文**判断。
_BOUNDARY_CASES = [
    ("值开头", f"{STUDENT_ID}", True),
    ("值开头 + 后续正文", f"{STUDENT_ID}的缓考申请", True),
    ("中文后（转义末位是数字）", f"学号{STUDENT_ID}", True),
    ("中文后（转义末位是字母）", f"是{STUDENT_ID}", True),
    ("拉丁字母后", f"ID{STUDENT_ID}", True),
    ("更长数字串中间（负控）", f"86{STUDENT_ID}", False),
]


@pytest.mark.parametrize("label,text,should_mask", _BOUNDARY_CASES)
def test_student_id_boundary_matrix(capture, label, text, should_mask):
    """边界矩阵：值开头 / 中文后 / `\\uXXXX` 后 / 拉丁字母后 / 更长数字串里。

    ⚠️ 负控（最后一条）与正控同等重要：12 位数字串里**不许**匹配出 10 位子串 ——
       那会把串从中间啃断，比不打码更糟。
    """
    client = observability.get_client()
    with client.start_as_current_observation(as_type="span", name="q") as s:
        s.update(input=[{"role": "user", "content": text}])
    client.flush()

    body = capture.body()
    assert body, "本地捕获服务器没收到任何东西 —— 说明数据根本没发出来"
    if should_mask:
        assert STUDENT_ID not in body, f"[{label}] 学号原样发出去了：\n{body[:500]}"
        assert "[REDACTED:student_id]" in body, f"[{label}] 打码标记没出现"
    else:
        assert text in body, f"[{label}] 长数字串被从中间啃掉了：\n{body[:500]}"


@pytest.mark.parametrize("label,text,secret", [
    ("学号", f"学号{STUDENT_ID}", STUDENT_ID),
    ("手机号", f"请拨打{PHONE}", PHONE),
    ("身份证", f"证件号{ID_CARD_FORMS[0]}", ID_CARD_FORMS[0]),
])
def test_every_numeric_rule_masks_inside_json_serialized_container(capture, label, text, secret):
    """三条数字规则共用同一个断词机制 —— 转义问题要**一起**修，不许只修学号。"""
    client = observability.get_client()
    with client.start_as_current_observation(as_type="span", name="q") as s:
        s.update(input=[{"role": "user", "content": text}])
    client.flush()

    body = capture.body()
    assert body, "本地捕获服务器没收到任何东西 —— 说明数据根本没发出来"
    assert secret not in body, f"[{label}] 原样发出去了：\n{body[:500]}"
    assert "[REDACTED:" in body, f"[{label}] 打码标记没出现"


def test_redacted_json_value_keeps_its_shape(capture):
    """★ 打码只许动标识符：宿主 JSON 的字段、顺序、其它正文一字不差。

    ⚠️ 为什么必须单独钉：匹配要在**解码后的正文**上做，再把结果编码回去 ——
       编码方式一旦变了（缩进 / `ensure_ascii=False` / 键顺序），
       云端拿到的就不是 SDK 原来那份 JSON，属于「另一种损坏」。
       期望值用 `json.dumps` 默认参数构造，正是 SDK 序列化容器的方式。
    """
    client = observability.get_client()
    payload = [
        {"role": "system", "content": "你是校园问答助手"},
        {"role": "user", "content": f"学号{STUDENT_ID}的学生，申请转专业需要满足哪些条件？"},
    ]
    with client.start_as_current_observation(
        as_type="generation", name="llm.generate", model="deepseek-flash",
    ) as gen:
        gen.update(input=payload)
    client.flush()

    body = capture.body()
    assert body, "本地捕获服务器没收到任何东西 —— 说明数据根本没发出来"
    expected = json.dumps(
        [payload[0],
         {"role": payload[1]["role"],
          "content": "学号[REDACTED:student_id]的学生，申请转专业需要满足哪些条件？"}],
        ensure_ascii=True,
    )
    assert expected in body, (
        "打码后的属性文本与期望不一致 —— 要么没打码，要么宿主 JSON 的形状被改了：\n"
        + body[:800]
    )
