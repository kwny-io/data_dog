"""providers 子系统的离线单元测试：配置读写（`config.py`）+ LLM 客户端（`client.py`）。

为什么要这样一个文件、且全部打桩
--------------------------------
`providers/config.py` 读写项目根的真实 `.env`，`providers/client.py` 会真的构造
`openai.OpenAI` 并发起网络请求。测试必须**零联网、不动真实 .env**：配置读写全部
指向 pytest 的 tmp 工程目录（内含 pyproject.toml 以固定 root），客户端则把
`openai.OpenAI` 换成记录入参的可编程桩——断言的是"我们交给 SDK 的 kwargs"与
"SDK 响应/异常被翻译成了什么"，这恰好就是该模块的全部职责。

覆盖的行为重点：
  - 配置回落链（默认供应商 → .env → 环境变量）与"未知供应商必须报错"；
  - 保存配置不覆盖用户已有变量（set_key 是增量写，不能整文件重写）；
  - 超时语义：显式参数 > DD_LLM_TIMEOUT_S > 默认 60s；0/负值 = 不限时（不带 timeout）；
  - 异常归一化：超时（含包在 __cause__ 里、含跨 SDK 类型名）→ TIMEOUT，其余 → UNAVAILABLE；
  - 用量回填：文本/工具调用两条返回形状，以及 usage_tracker 的 kind 分类。
"""
from __future__ import annotations

import json

import openai
import pytest
from dotenv import dotenv_values

from data_dog.errors import ToolErrorCode
from data_dog.observability import UsageTracker
from data_dog.providers.client import (
    ChatProvider,
    LLMCallError,
    LLMChatMessage,
    LLMToolParam,
    Provider,
    _is_timeout,
    _to_api,
    _to_api_tool,
)
from data_dog.providers.config import (
    config_path,
    ensure_key_from_env_or_file,
    env_key_name,
    find_project_root,
    load_config,
    load_env,
    save_config,
    saved_vendor,
)
from data_dog.providers.registry import BUILTIN_VENDORS, get_vendor

# ================================================================ 夹具 / 桩


@pytest.fixture()
def proj(tmp_path):
    """带 pyproject.toml 的临时"项目根"：固定 find_project_root 的结果。

    不这么做的话，find_project_root 会一直上溯到真实仓库根（本仓库就有
    pyproject.toml），测试就会写到真实 `.env` —— 那是不可接受的副作用。
    """
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'tmp'\n", encoding="utf-8")
    return root


class _Fn:
    """OpenAI tool_call.function 的最小替身。"""

    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, tid: str, name: str, arguments: str, type_: str = "function"):
        self.id = tid
        self.type = type_
        self.function = _Fn(name, arguments)


class _Msg:
    def __init__(self, content: str | None = None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, message: _Msg, finish_reason: str = "stop"):
        self.message = message
        self.finish_reason = finish_reason


class _Usage:
    def __init__(self, prompt_tokens: int, completion_tokens: int):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _Resp:
    """OpenAI ChatCompletion 的最小替身（只带 client.py 真正读的字段）。"""

    def __init__(self, message: _Msg, usage=None, finish_reason: str = "stop"):
        self.choices = [_Choice(message, finish_reason)]
        self.usage = usage


def _install_fake_openai(monkeypatch, responder):
    """把 `openai.OpenAI` 换成桩，返回被创建实例的列表。

    `Provider.__init__` 里是函数内 `from openai import OpenAI`，故打桩模块属性
    即可命中（不会在 import 期被提前绑定）。
    """
    created: list = []

    class _Completions:
        def __init__(self, sink: list):
            self._sink = sink

        def create(self, **kwargs):
            self._sink.append(kwargs)
            return responder(kwargs)

    class _Chat:
        def __init__(self, sink: list):
            self.completions = _Completions(sink)

    class _OpenAI:
        def __init__(self, **kwargs):
            self.init_kwargs = kwargs
            self.calls: list = []
            self.chat = _Chat(self.calls)
            created.append(self)

    monkeypatch.setattr(openai, "OpenAI", _OpenAI)
    return created


def _make_provider(monkeypatch, responder):
    created = _install_fake_openai(monkeypatch, responder)
    p = Provider(get_vendor("deepseek"), "sk-unit-test")
    return p, created[0]


def test_llm_latency_histogram_counts_both_success_and_failure(monkeypatch):
    """provider 调用必须进 `data_dog_llm_request_duration_seconds`——**成功与失败都进**。

    为什么失败也要进：慢请求往往以超时收场，只统计成功样本会把 P95 系统性低估。
    这条补上"同步路 P95/P99"问题的另一半——端点直方图量的是**端点**，这里量的是**模型**，
    排查"是模型慢还是我们慢"必须能分开看。
    """
    from prometheus_client import generate_latest

    from data_dog.core import metrics

    def _count(outcome: str) -> float:
        """按 vendor+outcome **求和**（同一 vendor 可能已有多个 model 序列——

        取"第一条匹配行"会在全量跑时因序列顺序不同而随机失败，这里不依赖顺序）。
        """
        total = 0.0
        for line in generate_latest(metrics.registry).decode().splitlines():
            if (line.startswith("data_dog_llm_request_duration_seconds_count")
                    and 'vendor="deepseek"' in line
                    and f'outcome="{outcome}"' in line):
                total += float(line.rsplit(" ", 1)[1])
        return total

    ok0, timeout0 = _count("ok"), _count("timeout")

    p, _ = _make_provider(monkeypatch, lambda kw: _Resp(_Msg("hi")))
    p.chat([LLMChatMessage(role="user", content="hi")])
    assert _count("ok") == ok0 + 1

    def _timeout(_kw):
        raise TimeoutError("read timeout")

    p2, _ = _make_provider(monkeypatch, _timeout)
    with pytest.raises(LLMCallError):
        p2.chat([LLMChatMessage(role="user", content="hi")])
    assert _count("timeout") == timeout0 + 1


# ================================================================ config：路径解析


def test_find_project_root_walks_up_to_pyproject(proj):
    nested = proj / "a" / "b"
    nested.mkdir(parents=True)
    assert find_project_root(nested) == proj.resolve()
    # 显式传 root 时不应继续上溯：自己就带 marker
    assert find_project_root(proj) == proj.resolve()
    print("PASS test_find_project_root_walks_up_to_pyproject")


def test_find_project_root_falls_back_to_start_dir(tmp_path):
    """上溯找不到 pyproject.toml → 返回起点自身（不是仓库根、也不抛）。"""
    start = tmp_path / "no_marker_here"
    start.mkdir()
    assert not any((p / "pyproject.toml").exists() for p in [start, *start.parents]), \
        "前置条件：tmp 目录链上本不该有 pyproject.toml"
    assert find_project_root(start) == start.resolve()
    print("PASS test_find_project_root_falls_back_to_start_dir")


def test_find_project_root_uses_cwd_by_default(proj, monkeypatch):
    monkeypatch.chdir(proj)
    assert find_project_root() == proj.resolve()
    assert config_path() == proj.resolve() / ".env"
    print("PASS test_find_project_root_uses_cwd_by_default")


def test_config_path_is_dotenv_under_root(proj):
    assert config_path(proj) == proj.resolve() / ".env"
    print("PASS test_config_path_is_dotenv_under_root")


# ================================================================ config：键名映射


def test_env_key_name_matches_registry_for_all_vendors():
    """每个内置供应商的 key 变量名必须来自注册表，不能各写一份（防漂移）。"""
    for vendor in BUILTIN_VENDORS:
        assert env_key_name(vendor.id) == vendor.requires_env_key
    assert env_key_name("deepseek") == "DEEPSEEK_API_KEY"
    print("PASS test_env_key_name_matches_registry_for_all_vendors")


def test_env_key_name_rejects_unknown_vendor():
    with pytest.raises(ValueError, match="未知供应商"):
        env_key_name("no-such-vendor")
    print("PASS test_env_key_name_rejects_unknown_vendor")


# ================================================================ config：读写


def test_load_env_missing_file_is_empty(proj):
    env, root = load_env(proj)
    assert env == {} and root == proj.resolve()
    print("PASS test_load_env_missing_file_is_empty")


def test_load_config_defaults_without_env_file(proj):
    cfg = load_config(proj)
    assert cfg == {"root": str(proj.resolve()), "vendor_id": "deepseek",
                   "model": "", "api_key": "", "base_url": ""}
    print("PASS test_load_config_defaults_without_env_file")


def test_save_then_load_roundtrip_and_preserves_other_keys(proj):
    """保存是**增量**写：用户已有的其它变量不得被抹掉。"""
    env_file = proj / ".env"
    env_file.write_text("UNRELATED=keep-me\n", encoding="utf-8")

    path = save_config("zhipu", "sk-zhipu-1", "glm-4-flash", proj)
    assert path == proj.resolve() / ".env"
    raw = dotenv_values(env_file)
    assert raw["UNRELATED"] == "keep-me"

    cfg = load_config(proj)
    assert cfg["vendor_id"] == "zhipu"
    assert cfg["api_key"] == "sk-zhipu-1"
    assert cfg["model"] == "glm-4-flash"
    print("PASS test_save_then_load_roundtrip_and_preserves_other_keys")


def test_save_config_without_model_keeps_previous_model(proj):
    """空 model 表示"不改模型"：既不该写空值，也不该删掉上次的模型。"""
    save_config("deepseek", "sk-1", "deepseek-chat", proj)
    save_config("deepseek", "sk-2", "", proj)
    values = dotenv_values(proj / ".env")
    assert values["DATA_DOG_MODEL"] == "deepseek-chat"
    assert values["DEEPSEEK_API_KEY"] == "sk-2"
    assert load_config(proj)["api_key"] == "sk-2"
    print("PASS test_save_config_without_model_keeps_previous_model")


def test_save_config_new_file_has_no_model_key(proj):
    save_config("openai", "sk-o", "", proj)
    values = dotenv_values(proj / ".env")
    assert values["DATA_DOG_VENDOR"] == "openai"
    assert values["OPENAI_API_KEY"] == "sk-o"
    assert "DATA_DOG_MODEL" not in values
    assert load_config(proj)["model"] == ""
    print("PASS test_save_config_new_file_has_no_model_key")


def test_load_config_blank_values_fall_back_to_empty(proj):
    """`KEY=` 这种显式空值在 dotenv 里解析成 None，必须回落成空串而非 None。"""
    (proj / ".env").write_text("DATA_DOG_VENDOR=openai\nOPENAI_API_KEY=\n"
                              "DATA_DOG_MODEL=\n", encoding="utf-8")
    cfg = load_config(proj)
    assert cfg["vendor_id"] == "openai"
    assert cfg["api_key"] == "" and cfg["model"] == ""
    print("PASS test_load_config_blank_values_fall_back_to_empty")


def test_load_config_unknown_vendor_fails_loudly(proj):
    """未知供应商不能静默当成 deepseek：那会让用户以为配好了、实际请求另一家。"""
    (proj / ".env").write_text("DATA_DOG_VENDOR=typo-vendor\n", encoding="utf-8")
    with pytest.raises(ValueError, match="未知供应商"):
        load_config(proj)
    print("PASS test_load_config_unknown_vendor_fails_loudly")


def test_saved_vendor_default_and_from_file(proj):
    assert saved_vendor(proj) == "deepseek"
    (proj / ".env").write_text("DATA_DOG_VENDOR=zhipu\n", encoding="utf-8")
    assert saved_vendor(proj) == "zhipu"
    print("PASS test_saved_vendor_default_and_from_file")


def test_ensure_key_prefers_process_env(proj, monkeypatch):
    """已存在的环境变量优先于 .env：避免覆盖用户 shell 里的配置。"""
    (proj / ".env").write_text("DEEPSEEK_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert ensure_key_from_env_or_file(proj) == "from-file"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    assert ensure_key_from_env_or_file(proj) == "from-env"
    print("PASS test_ensure_key_prefers_process_env")


def test_ensure_key_returns_empty_when_unset(proj, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert ensure_key_from_env_or_file(proj) == ""
    print("PASS test_ensure_key_returns_empty_when_unset")


def test_ensure_key_follows_saved_vendor_not_other_keys(proj, monkeypatch):
    """只认当前供应商的 key：不能把别家的 key 拿来当本家的用。"""
    (proj / ".env").write_text("DATA_DOG_VENDOR=openai\nOPENAI_API_KEY=sk-openai\n",
                               encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-not-be-used")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert ensure_key_from_env_or_file(proj) == "sk-openai"
    print("PASS test_ensure_key_follows_saved_vendor_not_other_keys")


# ================================================================ client：超时解析


def test_timeout_defaults_to_60s(monkeypatch):
    monkeypatch.delenv("DD_LLM_TIMEOUT_S", raising=False)
    _install_fake_openai(monkeypatch, lambda kw: _Resp(_Msg("x")))
    assert Provider(get_vendor("deepseek"), "k").timeout == 60.0
    print("PASS test_timeout_defaults_to_60s")


def test_timeout_reads_env_and_zero_means_unlimited(monkeypatch):
    _install_fake_openai(monkeypatch, lambda kw: _Resp(_Msg("x")))
    monkeypatch.setenv("DD_LLM_TIMEOUT_S", "12.5")
    assert Provider(get_vendor("deepseek"), "k").timeout == 12.5
    monkeypatch.setenv("DD_LLM_TIMEOUT_S", "0")
    assert Provider(get_vendor("deepseek"), "k").timeout is None
    print("PASS test_timeout_reads_env_and_zero_means_unlimited")


def test_explicit_timeout_overrides_env(monkeypatch):
    _install_fake_openai(monkeypatch, lambda kw: _Resp(_Msg("x")))
    monkeypatch.setenv("DD_LLM_TIMEOUT_S", "90")
    assert Provider(get_vendor("deepseek"), "k", timeout=3.5).timeout == 3.5
    print("PASS test_explicit_timeout_overrides_env")


def test_no_timeout_means_no_timeout_kwarg(monkeypatch):
    """不限时（timeout=None）时必须**不带** timeout 参数，否则 SDK 会用自己的默认值。"""
    monkeypatch.setenv("DD_LLM_TIMEOUT_S", "0")
    p, client = _make_provider(monkeypatch, lambda kw: _Resp(_Msg("ok")))
    p.chat([LLMChatMessage(role="user", content="hi")])
    assert "timeout" not in client.calls[0]
    print("PASS test_no_timeout_means_no_timeout_kwarg")


def test_timeout_kwarg_passed_to_sdk(monkeypatch):
    p, client = _make_provider(monkeypatch, lambda kw: _Resp(_Msg("ok")))
    p.chat([LLMChatMessage(role="user", content="hi")])
    assert client.calls[0]["timeout"] == p.timeout == 60.0
    print("PASS test_timeout_kwarg_passed_to_sdk")


# ================================================================ client：构造与注册表


def test_provider_uses_registry_base_url_and_api_key(monkeypatch):
    _install_fake_openai(monkeypatch, lambda kw: _Resp(_Msg("x")))
    p = Provider.from_config("zhipu", "sk-z")
    assert p.vendor.id == "zhipu"
    # base_url 必须来自注册表：否则会打到别家端点（静默错误）
    kw = p._client.init_kwargs
    assert kw["api_key"] == "sk-z"
    assert kw["base_url"] == get_vendor("zhipu").base_url
    # 不锁整字典：`max_retries` 这类"显式收紧 SDK 默认行为"的参数会随可靠性改造增减，
    # 锁死整字典会让正当改动弄红测试（同类教训见 tests/test_state_pg_unit.py 的建引擎断言）。
    # 用 get 容忍该参数将来被移除；一旦显式传了重试次数，就不允许放任 SDK 隐式重试
    # （SDK 默认 2 次重试且无墙钟上限，会把调用方的总预算撑成 ≈3×timeout）。
    assert kw.get("max_retries", 0) == 0
    print("PASS test_provider_uses_registry_base_url_and_api_key")


def test_from_config_unknown_vendor_rejected(monkeypatch):
    _install_fake_openai(monkeypatch, lambda kw: _Resp(_Msg("x")))
    with pytest.raises(ValueError, match="未知供应商"):
        Provider.from_config("nope", "k")
    print("PASS test_from_config_unknown_vendor_rejected")


def test_chat_provider_protocol_shape():
    """Protocol 是结构化契约：Provider 必须真的满足它（否则 AgentLoop 传参会崩）。"""

    def _accepts(p: ChatProvider) -> None:
        assert callable(p.chat)

    _accepts(Provider.__new__(Provider))  # 只做类型/结构校验，不构造 SDK 客户端
    assert isinstance(LLMChatMessage(role="user", content="x"), LLMChatMessage)
    print("PASS test_chat_provider_protocol_shape")


# ================================================================ client：文本路径


def test_chat_text_path_defaults_and_usage(monkeypatch):
    p, client = _make_provider(
        monkeypatch,
        lambda kw: _Resp(_Msg("诊断结论", tool_calls=None), usage=_Usage(11, 7),
                         finish_reason="stop"))
    out = p.chat([LLMChatMessage(role="system", content="sys"),
                  LLMChatMessage(role="user", content="问题")])

    sent = client.calls[0]
    assert sent["model"] == "deepseek-chat"          # 未指定 → 注册表默认模型
    assert sent["messages"] == [{"role": "system", "content": "sys"},
                                {"role": "user", "content": "问题"}]
    assert sent["temperature"] == 0.2 and sent["max_tokens"] == 2048
    assert "tools" not in sent and "tool_choice" not in sent
    assert out["kind"] == "text" and out["content"] == "诊断结论"
    assert out["finish_reason"] == "stop"
    assert out["__usage"]["prompt_tokens"] == 11
    assert out["__usage"]["completion_tokens"] == 7
    assert out["__usage"]["duration_ms"] >= 0.0
    print("PASS test_chat_text_path_defaults_and_usage")


def test_chat_explicit_model_and_params_passthrough(monkeypatch):
    p, client = _make_provider(monkeypatch, lambda kw: _Resp(_Msg("x")))
    p.chat([LLMChatMessage(role="user", content="q")], model="deepseek-reasoner",
           temperature=0.9, max_tokens=64)
    sent = client.calls[0]
    assert sent["model"] == "deepseek-reasoner"
    assert sent["temperature"] == 0.9 and sent["max_tokens"] == 64
    print("PASS test_chat_explicit_model_and_params_passthrough")


def test_chat_empty_content_becomes_empty_string(monkeypatch):
    """模型只调工具、不产文本时 content 为 None：返回必须是 "" 而非 None（下游直接拼串）。"""
    p, _ = _make_provider(monkeypatch, lambda kw: _Resp(_Msg(None)))
    assert p.chat([LLMChatMessage(role="user", content="q")])["content"] == ""
    print("PASS test_chat_empty_content_becomes_empty_string")


def test_chat_missing_usage_reports_zero(monkeypatch):
    """SDK 未返回 usage（部分兼容端点如此）→ 记 0，不得崩。"""
    p, _ = _make_provider(monkeypatch, lambda kw: _Resp(_Msg("x"), usage=None))
    usage = p.chat([LLMChatMessage(role="user", content="q")])["__usage"]
    assert usage["prompt_tokens"] == 0 and usage["completion_tokens"] == 0
    print("PASS test_chat_missing_usage_reports_zero")


# ================================================================ client：工具调用路径


def test_chat_with_tools_sends_openai_schema(monkeypatch):
    p, client = _make_provider(monkeypatch, lambda kw: _Resp(_Msg("x")))
    tool = LLMToolParam(name="query_evidence", description="查证据",
                        parameters={"type": "object", "properties": {}})
    p.chat([LLMChatMessage(role="user", content="q")], tools=[tool])
    sent = client.calls[0]
    assert sent["tools"] == [{"type": "function",
                              "function": {"name": "query_evidence",
                                           "description": "查证据",
                                           "parameters": {"type": "object",
                                                          "properties": {}}}}]
    assert sent["tool_choice"] == "auto"
    print("PASS test_chat_with_tools_sends_openai_schema")


def test_chat_empty_tools_list_omits_tool_kwargs(monkeypatch):
    """空列表等价于不传工具：不能带 `tools: []`（部分端点会 400）。"""
    p, client = _make_provider(monkeypatch, lambda kw: _Resp(_Msg("x")))
    p.chat([LLMChatMessage(role="user", content="q")], tools=[])
    assert "tools" not in client.calls[0] and "tool_choice" not in client.calls[0]
    print("PASS test_chat_empty_tools_list_omits_tool_kwargs")


def test_chat_tool_calls_path_maps_fields(monkeypatch):
    tc = _ToolCall("call_1", "query_evidence", '{"device_sn": "d1"}')
    p, _ = _make_provider(
        monkeypatch,
        lambda kw: _Resp(_Msg("我查一下", tool_calls=[tc]), usage=_Usage(3, 5),
                         finish_reason="tool_calls"))
    out = p.chat([LLMChatMessage(role="user", content="q")],
                 tools=[LLMToolParam("query_evidence", "d", {})])
    assert out["kind"] == "tool_calls"
    assert out["tool_calls"] == [{"id": "call_1", "name": "query_evidence",
                                  "arguments": '{"device_sn": "d1"}'}]
    # 原样回显给下一轮：协议要求保留 tool_calls 的完整结构
    assert out["__raw_tool_calls"] == [
        {"id": "call_1", "type": "function",
         "function": {"name": "query_evidence", "arguments": '{"device_sn": "d1"}'}}]
    assert out["content"] == "我查一下"
    assert out["__usage"]["completion_tokens"] == 5
    print("PASS test_chat_tool_calls_path_maps_fields")


# ================================================================ client：异常归一化


def test_chat_timeout_exception_becomes_timeout_code(monkeypatch):
    class APITimeoutError(Exception):
        pass

    def responder(kwargs):
        raise APITimeoutError("upstream hang")

    p, _ = _make_provider(monkeypatch, responder)
    with pytest.raises(LLMCallError) as ei:
        p.chat([LLMChatMessage(role="user", content="q")])
    assert ei.value.code is ToolErrorCode.TIMEOUT
    assert "超时" in str(ei.value) and "60.0" in str(ei.value)
    assert isinstance(ei.value.__cause__, APITimeoutError)
    print("PASS test_chat_timeout_exception_becomes_timeout_code")


def test_chat_other_exception_becomes_unavailable(monkeypatch):
    def responder(kwargs):
        raise ValueError("bad request")

    p, _ = _make_provider(monkeypatch, responder)
    with pytest.raises(LLMCallError) as ei:
        p.chat([LLMChatMessage(role="user", content="q")])
    assert ei.value.code is ToolErrorCode.UNAVAILABLE
    # 保留原始类型名与消息：排障时不能只剩一句"不可用"
    assert ei.value.detail == "ValueError: bad request"
    assert isinstance(ei.value.__cause__, ValueError)
    print("PASS test_chat_other_exception_becomes_unavailable")


def test_is_timeout_recognizes_builtin_and_sdk_type_names():
    class ReadTimeout(Exception):
        pass

    class ConnectTimeout(Exception):
        pass

    class TimeoutException(Exception):
        pass

    assert _is_timeout(TimeoutError("t"))
    assert _is_timeout(ReadTimeout("r"))
    assert _is_timeout(ConnectTimeout("c"))
    assert _is_timeout(TimeoutException("x"))
    assert not _is_timeout(ValueError("nope"))
    assert not _is_timeout(KeyError("nope"))
    print("PASS test_is_timeout_recognizes_builtin_and_sdk_type_names")


def test_is_timeout_follows_cause_chain_without_looping():
    """超时常被上一层库包一层：必须看 __cause__；自指 cause 不能无限递归。"""
    wrapper = RuntimeError("wrapped")
    wrapper.__cause__ = TimeoutError("inner")
    assert _is_timeout(wrapper)

    self_caused = RuntimeError("self")
    self_caused.__cause__ = self_caused
    assert not _is_timeout(self_caused)
    print("PASS test_is_timeout_follows_cause_chain_without_looping")


def test_llm_call_error_without_detail_uses_code_value():
    err = LLMCallError(ToolErrorCode.UNAVAILABLE)
    assert err.detail == "" and str(err) == ToolErrorCode.UNAVAILABLE.value
    print("PASS test_llm_call_error_without_detail_uses_code_value")


def test_chat_timeout_message_uses_configured_timeout(monkeypatch):
    monkeypatch.setenv("DD_LLM_TIMEOUT_S", "2.5")

    def responder(kwargs):
        raise TimeoutError("slow")

    p, _ = _make_provider(monkeypatch, responder)
    with pytest.raises(LLMCallError, match=r"2\.5s"):
        p.chat([LLMChatMessage(role="user", content="q")])
    print("PASS test_chat_timeout_message_uses_configured_timeout")


# ================================================================ client：用量与 ping


def test_usage_tracker_records_text_call(monkeypatch):
    p, _ = _make_provider(
        monkeypatch, lambda kw: _Resp(_Msg("x"), usage=_Usage(20, 5)))
    tracker = UsageTracker()
    p.chat([LLMChatMessage(role="user", content="q")], model="m-1",
           usage_tracker=tracker)
    assert tracker.call_count == 1
    stat = tracker.calls[0]
    assert (stat.model, stat.vendor_id) == ("m-1", "deepseek")
    assert (stat.prompt_tokens, stat.completion_tokens) == (20, 5)
    assert stat.kind == "text" and stat.duration_ms >= 0.0
    print("PASS test_usage_tracker_records_text_call")


def test_usage_tracker_records_tool_calls_kind(monkeypatch):
    tc = _ToolCall("c1", "t", "{}")
    p, _ = _make_provider(
        monkeypatch, lambda kw: _Resp(_Msg(None, tool_calls=[tc]), usage=_Usage(1, 2)))
    tracker = UsageTracker()
    p.chat([LLMChatMessage(role="user", content="q")], usage_tracker=tracker)
    assert tracker.calls[0].kind == "tool_calls"
    assert tracker.calls[0].model == "deepseek-chat"
    print("PASS test_usage_tracker_records_tool_calls_kind")


def test_usage_tracker_none_is_allowed(monkeypatch):
    p, _ = _make_provider(monkeypatch, lambda kw: _Resp(_Msg("x")))
    assert p.chat([LLMChatMessage(role="user", content="q")])["kind"] == "text"
    print("PASS test_usage_tracker_none_is_allowed")


def test_ping_true_on_success_and_false_on_any_error(monkeypatch):
    p, client = _make_provider(monkeypatch, lambda kw: _Resp(_Msg("pong")))
    assert p.ping(model="m") is True
    assert client.calls[0]["max_tokens"] == 4     # 探活必须便宜
    assert client.calls[0]["model"] == "m"

    def boom(kwargs):
        raise RuntimeError("down")

    p2, _ = _make_provider(monkeypatch, boom)
    assert p2.ping() is False                      # 任何异常都收敛成 False，不抛
    print("PASS test_ping_true_on_success_and_false_on_any_error")


# ================================================================ 消息转换


def test_to_api_tool_role_keeps_tool_call_id():
    d = _to_api(LLMChatMessage(role="tool", content="结果", name="query_evidence",
                               tool_call_id="call_9"))
    assert d == {"role": "tool", "tool_call_id": "call_9", "content": "结果"}
    assert "tool_calls" not in d and "name" not in d   # OpenAI 不收 name/tool_calls
    print("PASS test_to_api_tool_role_keeps_tool_call_id")


def test_to_api_echoes_assistant_tool_calls():
    raw = [{"id": "c1", "type": "function",
            "function": {"name": "t", "arguments": "{}"}}]
    d = _to_api(LLMChatMessage(role="assistant", content="", tool_calls=raw))
    assert d == {"role": "assistant", "content": "", "tool_calls": raw}
    # 未带 tool_calls 的普通消息不得凭空多出该字段
    assert _to_api(LLMChatMessage(role="user", content="hi")) == {
        "role": "user", "content": "hi"}
    print("PASS test_to_api_echoes_assistant_tool_calls")


def test_to_api_tool_schema_shape():
    d = _to_api_tool(LLMToolParam("f", "desc", {"type": "object"}))
    assert d == {"type": "function",
                 "function": {"name": "f", "description": "desc",
                              "parameters": {"type": "object"}}}
    assert json.loads(json.dumps(d)) == d          # 必须 JSON 可序列化（要过网线）
    print("PASS test_to_api_tool_schema_shape")
