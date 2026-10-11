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


def test_phone_is_masked(capture):
    client = observability.get_client()
    with client.start_as_current_observation(as_type="span", name="q") as s:
        s.update(input=f"联系方式 {PHONE}")
    client.flush()

    body = capture.body()
    assert PHONE not in body, "手机号原样发出去了"
    assert "[REDACTED:phone]" in body


def test_non_sensitive_text_survives(capture):
    """正控：脱敏不能把正常正文也抹掉（否则上面两条断言可能只是因为什么都没发）。"""
    client = observability.get_client()
    with client.start_as_current_observation(as_type="span", name="q") as s:
        s.update(input="图书馆开放时间是什么时候")
    client.flush()

    assert "图书馆开放时间" in capture.body(), "正常正文被误删了"
