"""`knowledge.py`（chroma-mcp 常驻 stdio 客户端）的离线测试。

分两层，理由不同：

1. **协议层用假进程**（`_FakeProc` 替掉 `subprocess.Popen`）。这一层要守的是
   JSON-RPC 细节：初始化握手校验 serverInfo、无 id 的通知不该等响应、无关注/
   非 JSON 噪声行要跳过、`error` 成员要抛、提前 EOF 要抛、isError 要抛、
   结果数组缺项/错位要兜住。这些分支**必须确定性触发**，而真 server 不会
   在你要的时候吐一条坏 JSON；所以这一层是替身，断言的是"我们怎么解析与失败"。

2. **真实服务用 `chroma-mcp.exe` 跑真检索**（临时数据目录、默认 ONNX 模型已
   缓存在本机时）。这一层证明"协议假设在真 chroma 上成立"：真返回的
   ids/documents/metadatas/distances 能被解析成命中项，来源既能取 metadata 也能
   从 `[来源: x]` 前缀提取，集合不存在时**失败关闭**（抛异常，不编造命中），
   且工具级错误不会打死常驻 server。模型未缓存时 skip——绝不允许测试触发联网下载。

之所以要把 `REQUEST_TIMEOUT_S` 的看门狗单测：它是"上游挂起不饿死 worker"的唯一
保障，父进程侧超时（sandbox 的 wall_s）看不到它的存在。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from data_dog import knowledge, paths, settings
from data_dog.knowledge import (
    DEFAULT_CHROMA_EXE,
    REQUEST_TIMEOUT_S,
    STARTUP_TIMEOUT_S,
    ChromaKnowledgeClient,
    KnowledgeRetrievalError,
    close_knowledge_server,
    query_knowledge,
)

# 默认 embedding 模型（chromadb 的 all-MiniLM-L6-v2）缓存位置：无缓存即跳过真机用例，
# 否则 chromadb 会去下载模型——测试绝不能联网。
_ONNX_MODEL = (Path.home() / ".cache" / "chroma" / "onnx_models"
               / "all-MiniLM-L6-v2" / "onnx" / "model.onnx")


# ================================================================ 假进程


class _FakeStdin:
    def __init__(self, proc: _FakeProc):
        self._proc = proc
        self.closed = False

    def write(self, text: str) -> None:
        self._proc._on_write(text)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        if self._proc.fail_close:
            raise OSError("pipe already closed")


class _FakeStdout:
    def __init__(self, proc: _FakeProc):
        self._proc = proc

    def readline(self) -> str:
        return self._proc._readline()


class _FakeProc:
    """`subprocess.Popen` 的替身：写进去的消息交给 responder，产生 stdout 行。

    responder(msg) -> 该消息应产生的 stdout 行（无 id 的通知返回空列表）。
    取空即 EOF（readline 返回 ""），用来触发"无响应"失败路径。
    """

    def __init__(self, responder=None, *, exit_code: int | None = None,
                 fail_close: bool = False, fail_wait: bool = False,
                 fail_kill: bool = False):
        self.responder = responder
        self.stdin = _FakeStdin(self)
        self.stdout = _FakeStdout(self)
        self.written: list[dict] = []
        self.lines: list[str] = []
        self.killed = False
        self.waits: list = []
        self.pid = 4242
        self.fail_close = fail_close
        self.fail_wait = fail_wait
        self.fail_kill = fail_kill
        self.returncode = exit_code

    # -- Popen 接口 --
    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.fail_wait:
            raise TimeoutError("还在跑")
        return self.returncode or 0

    def kill(self) -> None:
        if self.fail_kill:
            raise OSError("kill 失败")
        self.killed = True
        self.returncode = -9

    # -- 内部：消息进 / 行出 --
    def _on_write(self, text: str) -> None:
        for raw in text.splitlines():
            if not raw.strip():
                continue
            msg = json.loads(raw)
            self.written.append(msg)
            if self.responder is not None:
                self.lines.extend(self.responder(msg) or [])

    def _readline(self) -> str:
        return self.lines.pop(0) if self.lines else ""


def _make_responder(payload, *, server_name: str = "chroma", is_error: bool = False,
                    initialize_noise: tuple[str, ...] = ()):
    """标准 chroma responder：initialize 回 serverInfo，tools/call 回 JSON 文本。"""
    def respond(msg: dict) -> list[str]:
        method = msg.get("method")
        if method == "initialize":
            return [*initialize_noise, json.dumps(
                {"jsonrpc": "2.0", "id": msg["id"],
                 "result": {"serverInfo": {"name": server_name}}}, ensure_ascii=False)]
        if method == "tools/call":
            text = payload if isinstance(payload, str) else json.dumps(
                payload, ensure_ascii=False)
            return [json.dumps(
                {"jsonrpc": "2.0", "id": msg["id"],
                 "result": {"content": [{"type": "text", "text": text}],
                            "isError": is_error}}, ensure_ascii=False)]
        return []      # notifications/initialized：无 id，不该产生响应
    return respond


def _install_fake_popen(monkeypatch, procs: list[_FakeProc]) -> list[dict]:
    """按顺序交付伪造进程；记录每次 spawn 的 cmd/kwargs 供断言。"""
    spawns: list[dict] = []

    def fake_popen(cmd, **kwargs):
        idx = min(len(spawns), len(procs) - 1)
        spawns.append({"cmd": cmd, "kwargs": kwargs, "proc": procs[idx]})
        return procs[idx]

    monkeypatch.setattr(knowledge.subprocess, "Popen", fake_popen)
    return spawns


@pytest.fixture()
def exe(tmp_path) -> Path:
    """存在的假可执行文件（_start 只做 exists() 检查）。"""
    p = tmp_path / "chroma-mcp.exe"
    p.write_text("", encoding="utf-8")
    return p


def _query_payload(**overrides) -> dict:
    payload = {
        "ids": [["d1", "d2", "d3"]],
        "documents": [["[来源: 手册标题] 电池电压低于 20V 需充电",
                       "[来源: unitree白皮书] 关节电机温度阈值 75 摄氏度",
                       "既无 metadata 也无来源前缀的正文"]],
        "distances": [[0.11, 0.22, 0.33]],
        # d1：metadata 里的 source 应当**优先于**文本前缀
        "metadatas": [[{"source": "unitree/manual.md"}, {}, {}]],
    }
    payload.update(overrides)
    return payload


# ================================================================ 协议：握手与查询


def test_query_full_handshake_and_hits(monkeypatch, exe, tmp_path):
    """一次成功查询：spawn 参数、握手、notifications、命中解析全链路。"""
    data_dir = tmp_path / ".chroma"
    proc = _FakeProc(_make_responder(_query_payload()))
    spawns = _install_fake_popen(monkeypatch, [proc])
    client = ChromaKnowledgeClient(exe, data_dir)

    hits = client.query("电池电压", n_results=2)

    assert len(spawns) == 1
    assert spawns[0]["cmd"] == [str(exe), "--client-type", "persistent",
                                "--data-dir", str(data_dir)]
    kw = spawns[0]["kwargs"]
    assert kw["stdin"] is subprocess.PIPE and kw["stdout"] is subprocess.PIPE
    # stderr 仍**不污染父进程日志**；现在是落临时文件（为了失败时能取证，见 _describe_exit）。
    assert kw["stderr"] not in (None, subprocess.DEVNULL)
    assert hasattr(kw["stderr"], "fileno")
    assert kw["text"] is True and kw["encoding"] == "utf-8"
    assert kw["env"]["ANONYMIZED_TELEMETRY"] == "False"

    methods = [m["method"] for m in proc.written]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    init_params = proc.written[0]["params"]
    assert init_params["protocolVersion"] == "2024-11-05"
    assert init_params["clientInfo"]["name"] == "data_dog-kb"
    # 请求 id 递增：响应靠 id 配对，重复 id 会把上一条响应吃掉
    assert [m["id"] for m in proc.written if "id" in m] == [1, 2]

    call = proc.written[2]["params"]
    assert call["name"] == "chroma_query_documents"
    assert call["arguments"]["query_texts"] == ["电池电压"]
    assert call["arguments"]["n_results"] == 2
    assert call["arguments"]["collection_name"] == settings.kb_collection()

    assert hits[0] == {"id": "d1", "text": "[来源: 手册标题] 电池电压低于 20V 需充电",
                       "source": "unitree/manual.md", "distance": 0.11}
    # metadata 优先于文本前缀（两者都在时以 metadata 为准）
    assert hits[1]["source"] == "unitree白皮书"
    assert hits[1]["distance"] == 0.22 and hits[1]["id"] == "d2"
    # 两条来源路径都没有 → 明确的 unknown，不编造来源
    assert hits[2]["source"] == "unknown" and hits[2]["id"] == "d3"
    print("PASS test_query_full_handshake_and_hits")


def test_query_reuses_live_process_without_respawn(monkeypatch, exe, tmp_path):
    proc = _FakeProc(_make_responder(_query_payload()))
    spawns = _install_fake_popen(monkeypatch, [proc])
    client = ChromaKnowledgeClient(exe, tmp_path)
    client.query("q1")
    client.query("q2")
    assert len(spawns) == 1, "常驻 server 存活时不得重复 spawn"
    assert client.is_alive() is True
    print("PASS test_query_reuses_live_process_without_respawn")


def test_query_restarts_dead_worker_before_querying(monkeypatch, exe, tmp_path):
    """进程已死（poll 返回码）→ 先 close 旧进程再建新的；query 不能打到僵尸上。"""
    dead = _FakeProc(_make_responder(_query_payload()), exit_code=1)
    live = _FakeProc(_make_responder(_query_payload()))
    spawns = _install_fake_popen(monkeypatch, [live])
    client = ChromaKnowledgeClient(exe, tmp_path)
    client.proc = dead
    assert client.is_alive() is False

    hits = client.query("q")
    assert len(spawns) == 1 and client.proc is live
    assert dead.killed and dead.stdin.closed, "死进程必须被收尾（否则留僵尸/锁库）"
    assert hits[0]["id"] == "d1"
    print("PASS test_query_restarts_dead_worker_before_querying")


def test_query_source_unknown_and_short_arrays(monkeypatch, exe, tmp_path):
    """chroma 结果数组缺项/错位时逐项兜底：id 造 hitN、source=unknown、distance=None。"""
    payload = {"ids": [[]], "documents": [["无来源文本", "[来源: 白皮书] 有来源"]],
               "distances": [[]], "metadatas": [[]]}
    _install_fake_popen(monkeypatch, [_FakeProc(_make_responder(payload))])
    hits = ChromaKnowledgeClient(exe, tmp_path).query("q")

    assert [h["id"] for h in hits] == ["hit0", "hit1"]
    assert [h["source"] for h in hits] == ["unknown", "白皮书"]
    assert [h["distance"] for h in hits] == [None, None]
    print("PASS test_query_source_unknown_and_short_arrays")


def test_query_without_documents_returns_empty(monkeypatch, exe, tmp_path):
    """集合为空：payload 里根本没有 documents 键 → 返回 []，不得 KeyError。"""
    _install_fake_popen(monkeypatch, [_FakeProc(_make_responder({"ids": [[]]}))])
    assert ChromaKnowledgeClient(exe, tmp_path).query("q") == []
    print("PASS test_query_without_documents_returns_empty")


def test_query_tool_error_raises(monkeypatch, exe, tmp_path):
    """工具级 isError（如集合不存在）→ 必须抛，绝不返回"空命中"冒充查过了。"""
    _install_fake_popen(
        monkeypatch,
        [_FakeProc(_make_responder("Collection [x] does not exist", is_error=True))])
    client = ChromaKnowledgeClient(exe, tmp_path)
    with pytest.raises(KnowledgeRetrievalError, match="查询失败"):
        client.query("q")
    assert client.is_alive() is True, "工具级错误不是传输故障，不该杀掉常驻 server"
    print("PASS test_query_tool_error_raises")


def test_query_invalid_json_payload_keeps_preview(monkeypatch, exe, tmp_path):
    """响应不是 JSON → 报错要带原文前 300 字符（否则真实失败原因被吞掉）。"""
    _install_fake_popen(monkeypatch, [_FakeProc(_make_responder("database is locked"))])
    with pytest.raises(KnowledgeRetrievalError) as ei:
        ChromaKnowledgeClient(exe, tmp_path).query("q")
    assert "响应不是合法 JSON" in str(ei.value)
    assert "database is locked" in str(ei.value)
    print("PASS test_query_invalid_json_payload_keeps_preview")


# ================================================================ 协议：_recv 边界


def test_recv_skips_noise_blank_and_mismatched_ids(monkeypatch, exe, tmp_path):
    """噪声行（非 JSON / 空行 / 别人的 id）必须跳过，直到拿到自己的 id。

    空行用 "\\n"（真实 readline 对空行返回换行符，只有 EOF 才返回 ""），
    否则替身会把"空行"冒充成"进程退出"，测的就不是同一条分支了。
    """
    noise = ("这不是 JSON", "\n", json.dumps({"jsonrpc": "2.0", "id": 999,
                                              "result": {"serverInfo": {"name": "别的"}}}))
    _install_fake_popen(
        monkeypatch,
        [_FakeProc(_make_responder(_query_payload(), initialize_noise=noise))])
    # 若噪声没被跳过，initialize 会读到 id=999 那条而永远等不到 → 抛"无响应"
    hits = ChromaKnowledgeClient(exe, tmp_path).query("q")
    assert hits and hits[0]["id"] == "d1"
    print("PASS test_recv_skips_noise_blank_and_mismatched_ids")


def test_recv_eof_raises(monkeypatch, exe, tmp_path):
    """server 提前退出（stdout EOF）→ 明确报"无响应"，不无限阻塞。"""
    _install_fake_popen(monkeypatch, [_FakeProc(lambda msg: [])])
    with pytest.raises(KnowledgeRetrievalError, match="无响应"):
        ChromaKnowledgeClient(exe, tmp_path).query("q")
    print("PASS test_recv_eof_raises")


def test_recv_jsonrpc_error_member_raises(monkeypatch, exe, tmp_path):
    """JSON-RPC 层 error 成员 → 带原文抛出（区别于工具级 isError）。"""
    def responder(msg):
        if msg.get("method") == "initialize":
            return [json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                "error": {"code": -32602, "message": "参数非法"}})]
        return []

    _install_fake_popen(monkeypatch, [_FakeProc(responder)])
    with pytest.raises(KnowledgeRetrievalError, match="JSON-RPC 错误"):
        ChromaKnowledgeClient(exe, tmp_path).query("q")
    print("PASS test_recv_jsonrpc_error_member_raises")


def test_start_rejects_unexpected_server(monkeypatch, exe, tmp_path):
    """握手回来的 server 不是 chroma（比如 exe 指错了）→ 拒绝，不能凑合用。"""
    _install_fake_popen(
        monkeypatch,
        [_FakeProc(_make_responder(_query_payload(), server_name="filesystem"))])
    with pytest.raises(KnowledgeRetrievalError, match="非预期的 server"):
        ChromaKnowledgeClient(exe, tmp_path).query("q")
    print("PASS test_start_rejects_unexpected_server")


def test_start_missing_exe_raises_with_path(monkeypatch, tmp_path):
    """可执行文件不存在 → 报错并带上路径（比 OSError 的 errno 可诊断）。"""
    def exploding_popen(*a, **k):
        raise AssertionError("exe 不存在时不该尝试 spawn")

    monkeypatch.setattr(knowledge.subprocess, "Popen", exploding_popen)
    client = ChromaKnowledgeClient(tmp_path / "缺失.exe", tmp_path)
    with pytest.raises(KnowledgeRetrievalError, match="chroma-mcp 不存在"):
        client.query("q")
    assert client.proc is None
    print("PASS test_start_missing_exe_raises_with_path")


# ================================================================ 收尾与看门狗


def test_close_swallows_pipe_errors_and_is_idempotent(monkeypatch, exe, tmp_path):
    """收尾必须 best-effort：close/wait/kill 任一失败都不能把清理流程炸掉。"""
    proc = _FakeProc(_make_responder(_query_payload()), fail_close=True,
                     fail_wait=True, fail_kill=True)
    _install_fake_popen(monkeypatch, [proc])
    client = ChromaKnowledgeClient(exe, tmp_path)
    client.proc = proc

    client.close()
    assert client.proc is None, "即使 kill 失败也要把引用清掉（不能留着当活的用）"
    client.close()          # 幂等：proc 已是 None，直接返回
    print("PASS test_close_swallows_pipe_errors_and_is_idempotent")


def test_kill_is_noop_without_proc_and_swallows_kill_error(monkeypatch, exe, tmp_path):
    """_kill 是看门狗回调：无进程时是空操作，kill 失败也必须安静（在定时器线程里）。"""
    client = ChromaKnowledgeClient(exe, tmp_path)
    client._kill()          # proc is None → 不应抛

    client.proc = _FakeProc(_make_responder(_query_payload()), fail_kill=True)
    client._kill()          # kill 抛 OSError → 被吞
    print("PASS test_kill_is_noop_without_proc_and_swallows_kill_error")


class _RecordingTimer:
    """替掉 threading.Timer：只记录"是否启动/取消"，不真的开线程。"""

    created: list[_RecordingTimer] = []

    def __init__(self, interval, function):
        self.interval = interval
        self.function = function
        self.started = False
        self.cancelled = False
        self.daemon = False
        _RecordingTimer.created.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


def test_request_arms_watchdog_and_cancels_it(monkeypatch, exe, tmp_path):
    """每次请求都要挂上 60s 看门狗杀进程，并在返回前取消（否则线程泄漏+误杀下次请求）。"""
    _RecordingTimer.created = []
    monkeypatch.setattr(knowledge.threading, "Timer", _RecordingTimer)
    _install_fake_popen(monkeypatch, [_FakeProc(_make_responder(_query_payload()))])
    ChromaKnowledgeClient(exe, tmp_path).query("q")

    timers = _RecordingTimer.created
    # 握手（进程+模型冷启动）拿更宽的预算，普通调用保持 60s——这条把两者的**区别**钉成契约。
    assert [t.interval for t in timers] == [STARTUP_TIMEOUT_S, REQUEST_TIMEOUT_S]
    assert all(t.started and t.daemon and t.cancelled for t in timers)
    print("PASS test_request_arms_watchdog_and_cancels_it")


def test_handshake_gets_a_wider_budget_than_calls():
    """冷启动预算必须**严格大于**普通调用预算：否则负载下的慢启动被当成挂起并被 kill。"""
    from data_dog.knowledge import _timeout_for

    assert _timeout_for("initialize") == STARTUP_TIMEOUT_S
    assert _timeout_for("tools/call") == REQUEST_TIMEOUT_S
    assert STARTUP_TIMEOUT_S > REQUEST_TIMEOUT_S
    print("PASS test_handshake_gets_a_wider_budget_than_calls")


def test_dead_child_error_carries_exit_code_and_stderr(monkeypatch, exe, tmp_path):
    """子进程崩了：报错要带**退出码**与 **stderr 尾巴**，不能只有一句"无响应"。

    此前 stderr 被丢进 DEVNULL，全量跑偶发失败时完全无法定位（是崩了？还是被我们 kill 的？）。
    """
    import tempfile as _tf

    _RecordingTimer.created = []
    monkeypatch.setattr(knowledge.threading, "Timer", _RecordingTimer)
    # 先抓住原函数：knowledge.tempfile 就是全局 tempfile 模块，直接调会被自己递归。
    orig_tmpfile = _tf.TemporaryFile

    def _fake_tmpfile(*args, **kwargs):
        fh = orig_tmpfile(*args, **kwargs)
        fh.write(b"onnxruntime: model.onnx not found\n")
        fh.seek(0)
        return fh

    monkeypatch.setattr(knowledge.tempfile, "TemporaryFile", _fake_tmpfile)
    _install_fake_popen(monkeypatch, [_FakeProc(lambda msg: [], exit_code=3)])
    client = ChromaKnowledgeClient(exe, tmp_path)
    with pytest.raises(KnowledgeRetrievalError) as exc:
        client.query("q")
    msg = str(exc.value)
    assert "退出码 3" in msg
    assert "进程自行退出" in msg
    assert "model.onnx not found" in msg
    print("PASS test_dead_child_error_carries_exit_code_and_stderr")


def test_request_cancels_watchdog_even_on_failure(monkeypatch, exe, tmp_path):
    """请求失败也要取消看门狗（finally）：否则 60s 后误杀一个已经复用的新进程。"""
    _RecordingTimer.created = []
    monkeypatch.setattr(knowledge.threading, "Timer", _RecordingTimer)
    _install_fake_popen(
        monkeypatch, [_FakeProc(_make_responder("boom", is_error=True))])
    with pytest.raises(KnowledgeRetrievalError):
        ChromaKnowledgeClient(exe, tmp_path).query("q")
    assert all(t.cancelled for t in _RecordingTimer.created)
    print("PASS test_request_cancels_watchdog_even_on_failure")


# ================================================================ 设置来源


def test_chroma_exe_prefers_setting_then_default(monkeypatch):
    monkeypatch.delenv("DD_CHROMA_MCP_EXE", raising=False)
    assert knowledge._chroma_exe() == DEFAULT_CHROMA_EXE
    monkeypatch.setenv("DD_CHROMA_MCP_EXE", r"D:\other\chroma-mcp.exe")
    assert knowledge._chroma_exe() == Path(r"D:\other\chroma-mcp.exe")
    print("PASS test_chroma_exe_prefers_setting_then_default")


def test_collection_follows_setting(monkeypatch):
    monkeypatch.setenv("DD_KB_COLLECTION", "unitree_g1")
    assert knowledge._collection() == "unitree_g1"
    print("PASS test_collection_follows_setting")


# ================================================================ 模块级常驻入口


def _install_fake_client(monkeypatch, plans: list[list]):
    """把 ChromaKnowledgeClient 换成假类；plans[i] 是第 i 个实例的 query 行为序列。"""
    created: list = []

    class _Client:
        def __init__(self, exe_path, data_dir):
            self.exe_path = exe_path
            self.data_dir = data_dir
            self.calls: list[tuple] = []
            self.closes = 0
            self._plan = list(plans[len(created)])
            created.append(self)

        def query(self, question, n_results=3):
            self.calls.append((question, n_results))
            outcome = self._plan.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        def close(self):
            self.closes += 1

    monkeypatch.setattr(knowledge, "ChromaKnowledgeClient", _Client)
    return created


@pytest.fixture()
def _isolate_persistent(monkeypatch):
    """隔离模块级单例：query_knowledge 会改 _PERSISTENT，测完必须还原。"""
    monkeypatch.setattr(knowledge, "_PERSISTENT", None)
    return monkeypatch


def test_query_knowledge_reuses_client_when_singleton_present(_isolate_persistent,
                                                             tmp_path):
    """单例已存在时必须直接复用（这条是 `query_knowledge` 承诺的常驻语义）。"""
    monkeypatch = _isolate_persistent
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    created = _install_fake_client(monkeypatch, [[["hit1"]]])
    existing = knowledge.ChromaKnowledgeClient(Path("fake.exe"), tmp_path)  # 即假类
    monkeypatch.setattr(knowledge, "_PERSISTENT", existing)

    assert query_knowledge("q") == ["hit1"]
    assert len(created) == 1, "已有常驻客户端时不得新建"
    assert existing.calls == [("q", 3)]
    assert existing.closes == 0
    print("PASS test_query_knowledge_reuses_client_when_singleton_present")


def test_query_knowledge_cold_success_caches_client(_isolate_persistent, tmp_path):
    """冷启动**成功路径也要写回常驻**（否则每次都新起 chroma-mcp，并泄漏那个进程）。

    这条曾经锁定一个真实缺陷：`_PERSISTENT = client` 只写在"失败后重试"的 `finally` 里，
    冷启动成功返回时不缓存。后果有两个：
      1. 常驻复用失效——每次查询都重新 spawn 一个 chroma-mcp（多花数秒 + 重复开库）；
      2. 那个进程从此无人引用，`close_knowledge_server()`/atexit 都关不到它 →
         泄漏僵尸 server 占着 SQLite 锁（正是该函数注释里担心的间歇性故障）。
    现在成功即缓存：第二次调用必须**复用**同一个客户端。
    """
    monkeypatch = _isolate_persistent
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    # 只应创建 1 个实例，而它会被查两次 → plans[0] 要排两条结果
    created = _install_fake_client(monkeypatch, [[["hit1"], ["hit1"]]])

    assert query_knowledge("q1") == ["hit1"]
    assert knowledge._PERSISTENT is created[0], "冷启动成功必须写回常驻引用"
    assert query_knowledge("q2") == ["hit1"]
    assert len(created) == 1, "第二次调用应复用常驻客户端，而不是又新建一个"
    print("PASS test_query_knowledge_cold_success_caches_client")


def test_query_knowledge_rebuilds_once_after_failure(_isolate_persistent, tmp_path):
    """首次失败 → 关掉旧客户端并**只重试一次**；成功的新客户端成为常驻。"""
    monkeypatch = _isolate_persistent
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    boom = KnowledgeRetrievalError("server 挂了")
    created = _install_fake_client(monkeypatch, [[boom], [["hit2"]]])

    assert query_knowledge("q") == ["hit2"]
    assert len(created) == 2
    assert created[0].closes == 1, "失败路径必须关掉旧进程（防僵尸 server 锁库）"
    assert knowledge._PERSISTENT is created[1]
    print("PASS test_query_knowledge_rebuilds_once_after_failure")


def test_query_knowledge_propagates_second_failure(_isolate_persistent, tmp_path):
    """重试仍失败 → 异常向上抛（调用方决定降级），但常驻引用要指向那个坏客户端。"""
    monkeypatch = _isolate_persistent
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    created = _install_fake_client(
        monkeypatch,
        [[KnowledgeRetrievalError("第一次")], [KnowledgeRetrievalError("第二次")]])

    with pytest.raises(KnowledgeRetrievalError, match="第二次"):
        query_knowledge("q")
    assert len(created) == 2, "只允许重试一次，不得无限重建"
    assert knowledge._PERSISTENT is created[1]
    print("PASS test_query_knowledge_propagates_second_failure")


def test_close_knowledge_server_is_idempotent(_isolate_persistent):
    monkeypatch = _isolate_persistent
    calls: list[int] = []

    class _C:
        def close(self):
            calls.append(1)

    monkeypatch.setattr(knowledge, "_PERSISTENT", _C())
    close_knowledge_server()
    assert calls == [1] and knowledge._PERSISTENT is None
    close_knowledge_server()          # 无单例时是空操作（atexit 会再调一次）
    assert calls == [1]
    print("PASS test_close_knowledge_server_is_idempotent")


# ================================================================ 真机：真 chroma-mcp

@pytest.mark.skipif(not DEFAULT_CHROMA_EXE.exists(),
                    reason=f"未安装 chroma-mcp（{DEFAULT_CHROMA_EXE}）")
@pytest.mark.skipif(not _ONNX_MODEL.exists(),
                    reason="默认 embedding 模型未缓存，跳过以免测试触发联网下载")
def test_real_chroma_mcp_query_and_fail_closed(tmp_path, monkeypatch):
    """真 server + 临时数据目录：真检索能解析出命中，集合不存在时失败关闭。

    用 `_request` 直接喂两个集合（这也是 `query()` 走的同一条通道）：一个把来源放
    metadata，一个只把来源写在 `[来源: x]` 文本前缀里——两条来源提取路径都要在
    **真实响应形状**下验证。最后用一个不存在的集合验证失败关闭，且工具级错误不能
    打死常驻 server（父进程还要继续用它查别的）。
    """
    monkeypatch.setattr(settings, "kb_collection", lambda: "dd_kb_meta")
    client = ChromaKnowledgeClient(DEFAULT_CHROMA_EXE, tmp_path / "chroma")
    try:
        client._start()
        assert client.is_alive(), "真 chroma-mcp 应完成握手并常驻"

        for name, ids, docs, metas in [
            ("dd_kb_meta", ["m1", "m2"],
             ["电池电压低于 20V 时需回充", "关节电机温度阈值为 75 摄氏度"],
             [{"source": "unitree/manual.md"}, {"source": "unitree/thermal.md"}]),
            ("dd_kb_prefix", ["p1"],
             ["[来源: unitree白皮书] 关节电机堵转保护会在 3 秒内断电"], None),
        ]:
            args = {"collection_name": name, "ids": ids, "documents": docs}
            if metas is not None:
                args["metadatas"] = metas
            client._request("tools/call",
                            {"name": "chroma_create_collection",
                             "arguments": {"collection_name": name}})
            client._request("tools/call",
                            {"name": "chroma_add_documents", "arguments": args})

        hits = client.query("电池电压过低", n_results=2)
        assert len(hits) == 2
        assert {h["source"] for h in hits} == {"unitree/manual.md", "unitree/thermal.md"}
        assert all(h["id"] and h["text"] and isinstance(h["distance"], float)
                   for h in hits)

        monkeypatch.setattr(settings, "kb_collection", lambda: "dd_kb_prefix")
        prefix_hits = client.query("关节电机堵转保护", n_results=1)
        assert prefix_hits[0]["source"] == "unitree白皮书"

        monkeypatch.setattr(settings, "kb_collection", lambda: "dd_kb_missing")
        with pytest.raises(KnowledgeRetrievalError, match="查询失败"):
            client.query("任意")
        assert client.is_alive(), "工具级错误后 server 仍应可用（否则每次失败都要重启）"
    finally:
        client.close()
    assert client.is_alive() is False and client.proc is None
    print("PASS test_real_chroma_mcp_query_and_fail_closed")
