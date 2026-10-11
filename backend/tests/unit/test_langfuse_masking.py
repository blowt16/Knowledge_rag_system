"""脱敏护栏（【方案 §7 / §12.1】#3）。

**实测确认的问题**：日志那边脱敏做了，但 span 是另一个存储。
Langfuse 默认会记录 input/output —— 后果是「日志干净了，云端却摆着学号姓名」。

本测试用**本地捕获服务器**断言实际发出的字节，不是断言 SDK 的自我声明。
"""
from __future__ import annotations

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
