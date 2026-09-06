# -*- coding: utf-8 -*-
"""A4 接入测试：Agent 通过自研 context-injector MCP 获取可信上下文。

覆盖四条路径：
1. MCP 客户端能取回并校验可信上下文（含来源校验、越权判定）
2. 启用 MCP 后，设备一致的候选正常调查
3. 候选设备与可信上下文不一致 → 失败关闭（needs_human_review）
4. server 不可用 → 失败关闭（不降级为无鉴权）
"""
import os

import pytest

from data_dog.agent.loop import AgentLoop
from data_dog.evidence.store import EvidenceStore
from data_dog.mcp_context import fetch_trusted_context
from data_dog.tools.investigation import InvestigationTools

from .test_investigation import FakeProvider, make_candidate


@pytest.fixture()
def mcp_enabled():
    """启用 DD_CTX_MCP，测完还原环境并关闭常驻 server。"""
    from data_dog import mcp_context as mc
    old = os.environ.get("DD_CTX_MCP")
    os.environ["DD_CTX_MCP"] = "1"
    yield
    mc.close_persistent_context_server()
    if old is None:
        os.environ.pop("DD_CTX_MCP", None)
    else:
        os.environ["DD_CTX_MCP"] = old


def test_fetch_trusted_context_from_mcp():
    ctx = fetch_trusted_context()
    assert ctx.tenant_id == "tenant-default"
    assert ctx.device_sn == "device-abc123"
    assert "joint_motor.front_left_hip" in ctx.allowed_components
    # 授权判定正常工作
    assert ctx.can_access(ctx.device_sn, "battery.main")
    assert not ctx.can_access(ctx.device_sn, "hacker.zzz")
    print("PASS test_fetch_trusted_context_from_mcp")


def test_persistent_server_reused_across_fetches(mcp_enabled):
    """常驻复用：多次 fetch 用同一 server 进程；显式关闭后重建新进程。"""
    from data_dog import mcp_context as mc
    ctx1 = mc.fetch_trusted_context()
    pid1 = mc._PERSISTENT.proc.pid
    ctx2 = mc.fetch_trusted_context()
    pid2 = mc._PERSISTENT.proc.pid
    assert pid1 == pid2, "第二次 fetch 应复用常驻进程，而不是重新 spawn"
    assert ctx2.device_sn == ctx1.device_sn
    mc.close_persistent_context_server()
    assert mc._PERSISTENT is None
    mc.fetch_trusted_context()
    pid3 = mc._PERSISTENT.proc.pid
    assert pid3 != pid1, "关闭后应重建新进程"
    print("PASS test_persistent_server_reused_across_fetches")


def test_agent_with_mcp_context(mcp_enabled):
    fp = FakeProvider()
    agent = AgentLoop(fp, InvestigationTools(EvidenceStore()))
    report = agent.investigate(make_candidate())  # 设备与上下文一致 → 正常调查
    assert report.disposition == "confirmed"
    assert fp.calls == 3  # 正常走完查证据→查知识→结论
    print("PASS test_agent_with_mcp_context")


def test_agent_fail_closed_on_device_mismatch(mcp_enabled):
    agent = AgentLoop(FakeProvider(), InvestigationTools(EvidenceStore()))
    cand = make_candidate()
    cand.device_sn = "device-EVIL"  # 与可信上下文设备不一致
    report = agent.investigate(cand)
    assert report.disposition == "needs_human_review"
    assert "不一致" in report.summary
    print("PASS test_agent_fail_closed_on_device_mismatch")


def test_agent_fail_closed_on_server_missing(mcp_enabled):
    old = os.environ.get("DD_CTX_MCP_SERVER")
    os.environ["DD_CTX_MCP_SERVER"] = "Z:/definitely/not/here.py"
    try:
        agent = AgentLoop(FakeProvider(), InvestigationTools(EvidenceStore()))
        report = agent.investigate(make_candidate())
        assert report.disposition == "needs_human_review"
        assert "失败关闭" in report.summary
    finally:
        if old is None:
            os.environ.pop("DD_CTX_MCP_SERVER", None)
        else:
            os.environ["DD_CTX_MCP_SERVER"] = old
    print("PASS test_agent_fail_closed_on_server_missing")
