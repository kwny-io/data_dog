"""provider 接入的凭据不泄露：脱敏在**源头**做，另在错误出口与报告落库前各兜一层。

为什么这些断言重要
------------------
provider 的 SDK 异常消息可能带 `Authorization: Bearer <key>` / `api_key=<key>`，多模态
请求还可能带整段 `data:image/...;base64,...`。这些 detail 会流到 **HTTP 错误体**（直接发给
调用方）与**调查报告**（持久化并可从 `/v1/investigations` 读回）。所以脱敏必须发生在
**归一化那一处**，而不是靠每个调用方自觉。

约定：每个 test 结尾 print("PASS <name>")。
"""
from __future__ import annotations

import sys
import types

import pytest

from data_dog.agent.loop import AgentLoop
from data_dog.core.redact import redact
from data_dog.errors import ToolErrorCode
from data_dog.providers.client import LLMCallError, LLMChatMessage, Provider
from data_dog.schemas.events import CandidateEvent
from data_dog.service.deps import _error_body
from data_dog.tools.investigation import InvestigationTools

KEY = "sk-live-abcdef1234567890"
COMP = "joint_motor.front_left_hip"


class _Store:
    def query_telemetry(self, *a, **k):
        return None

    def query_vendor_knowledge(self, *a, **k):
        return []

    def query_neighbor_events(self, *a, **k):
        return []

    def query_recent_changes(self, *a, **k):
        return {"changes": []}


def _provider_with(exc: Exception, key: str = KEY) -> Provider:
    p = Provider.from_config("deepseek", key)

    def create(**kw):
        raise exc

    p._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    return p


def test_redact_by_value_and_patterns():
    assert KEY not in redact(f"boom {KEY}", secrets=[KEY])
    s = redact("Authorization: Bearer sk-xyz123456789")
    assert "sk-xyz123456789" not in s
    s2 = redact("GET /x?api_key=sk-abcdef123456 failed")
    assert "sk-abcdef123456" not in s2
    s3 = redact("secret=topsecretvalue; password=hunter2abc")
    assert "topsecretvalue" not in s3 and "hunter2abc" not in s3
    s4 = redact("body=data:image/png;base64," + "A" * 400)
    assert "base64," not in s4
    s5 = redact("blob " + "B" * 800)
    assert "B" * 300 not in s5
    assert len(redact("x" * 5000)) <= 520          # 截断，别让详情无界增长
    assert redact("ValueError: bad request") == "ValueError: bad request"   # 不误伤
    assert redact(None) == "" and redact(123) == "123"
    print("PASS test_redact_by_value_and_patterns")


def test_sdk_exception_detail_is_redacted():
    exc = RuntimeError(f"401 Unauthorized | Authorization: Bearer {KEY} | see docs")
    p = _provider_with(exc)
    with pytest.raises(LLMCallError) as ei:
        p.chat([LLMChatMessage(role="user", content="q")])
    assert ei.value.code is ToolErrorCode.UNAVAILABLE
    assert KEY not in ei.value.detail
    assert KEY not in str(ei.value)
    assert isinstance(ei.value.__cause__, RuntimeError)   # 本地排障仍拿得到原异常
    # 良性消息原样保留（排障信息不能被脱敏吃掉）
    p2 = _provider_with(ValueError("bad request"))
    with pytest.raises(LLMCallError) as ei2:
        p2.chat([LLMChatMessage(role="user", content="q")])
    assert ei2.value.detail == "ValueError: bad request"
    print("PASS test_sdk_exception_detail_is_redacted")


def test_error_body_redacts_detail():
    body = _error_body(ToolErrorCode.UNAVAILABLE, f"boom Authorization: Bearer {KEY}")
    assert KEY not in body["detail"]
    ok = _error_body(ToolErrorCode.INVALID_INPUT, "缺必需字段")
    assert ok["detail"] == "缺必需字段"
    print("PASS test_error_body_redacts_detail")


def test_fail_closed_report_redacts_reason():
    class _P:
        def chat(self, *a, **k):
            raise AssertionError("不应被调用")

    agent = AgentLoop(_P(), InvestigationTools(_Store()))
    cand = CandidateEvent(candidate_id="c", device_sn="d", component_ref=COMP,
                          signal="s", severity_hint="warning",
                          window_start="a", window_end="b", sample_count=1)
    rep = agent._fail_closed(cand, f"模型调用失败: Authorization: Bearer {KEY}")
    blob = rep.summary + " " + " ".join(rep.missing_evidence)
    assert KEY not in blob
    assert rep.disposition == "needs_human_review"      # 失败关闭语义不变
    print("PASS test_fail_closed_report_redacts_reason")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 权限语义")
def test_save_config_sets_0600(tmp_path):
    from data_dog.providers.config import save_config
    p = save_config("deepseek", "sk-test-1234567890", "deepseek-chat", root=tmp_path)
    assert (p.stat().st_mode & 0o777) == 0o600
    print("PASS test_save_config_sets_0600")


def test_redact_masks_password_inside_connection_url():
    """连接串里的口令必须遮掉（DSN 几乎总会出现在数据库异常消息里）。

    为什么单独测：`/readyz` 是**公开探针**。SQLAlchemy/psycopg 的报错形如
    `... (postgresql+psycopg://data_dog:hunter2@db:5432/data_dog)`，而既有的
    `password=xxx` 类规则只认 `key=value`，认不出 URL 里的 `:password@`
    —— 于是初始口令会原样回给任何未认证调用方。2026-10-25 由
    `test_startup_migration_visibility.py::test_startup_error_is_redacted` 抓出。
    """
    out = redact("could not connect (postgresql+psycopg://data_dog:hunter2@db:5432/data_dog)")
    assert "hunter2" not in out, out
    # 只遮口令：宿主/库名要留着，否则排查时看不出连的是谁
    assert "db:5432" in out and "data_dog" in out, out
