"""真实遥测流接入测试：file 尾随 / HTTP 推送 / runner 落库 / 源工厂。

为什么这些断言重要
------------------
流式接入是"真实遥测"的落点。若它悄悄丢数据（坏行静默跳过、半行被当坏行、
按错租户落库），现象是"候选少了"——最难排查的一类静默错误。故这里逐条钉住：
- file：坏行跳过但**不中断**、半行等补全、截断/轮转从头读；
- http：头注入身份、正文身份被覆盖、路径/大小校验；
- runner：**按租户分组**落库（两个租户的消息不串台）；
- 工厂：四类源的配置装配与未知类型的清晰报错。

离线：file/http 用标准库；mqtt/kafka 依赖缺失，只断言"缺依赖时报错清晰"。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from data_dog.data_plane.p2_demo import _detector_config
from data_dog.data_plane.pipeline import DataPlane, build_demo_resolver
from data_dog.stream.base import StreamMessage
from data_dog.stream.factory import build_source
from data_dog.stream.file import FileTailSource, resolve_watch_path
from data_dog.stream.runner import StreamRunner

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def plane_factory():
    """造一个用离线 demo 绑定的 DataPlane（含共享候选构建器）。"""
    from data_dog.data_plane.candidate_shared import SharedCandidateBuilder
    from data_dog.state.memory import InMemoryStateStore

    def make():
        return DataPlane(build_demo_resolver(_detector_config()),
                         builder=SharedCandidateBuilder(
                             state_store=InMemoryStateStore()))
    return make


def _raw_line(event_id, temp=95, device_field=None):
    body = {"event_id": event_id, "occurred_at": "2026-09-13T10:00:00Z",
            "received_at": "2026-09-13T10:00:00.1Z", "sequence": 1,
            "vendor": "unitree", "model": "go2", "firmware": "3.2.1",
            "protocol_version": "motor-log-v2",
            "component_hint": "joint_motor.front_left_hip",
            "payload_inline": json.dumps({"temperature": temp, "current": 12.0,
                                          "mode": 0})}
    if device_field:
        body["device_sn"] = device_field
    return json.dumps(body)


# ---------------------------------------------------------------- file 源

def test_file_tail_reads_new_lines(tmp_path):
    f = tmp_path / "robot.log"
    f.write_text(_raw_line("e1") + "\n", encoding="utf-8")
    src = FileTailSource(str(f), tenant_id="t1", device_sn="dev-1",
                         start_at_end=True)          # 从末尾开始，忽略已有内容
    assert src.poll(0.0) == []                       # 已存在的那行不重读

    with open(f, "a", encoding="utf-8") as fh:
        fh.write(_raw_line("e2") + "\n")
    msgs = src.poll(0.0)
    assert len(msgs) == 1 and msgs[0].payload["event_id"] == "e2"
    assert msgs[0].auth == {"tenant_id": "t1", "device_sn": "dev-1",
                            "principal": "file-tail"}


def test_file_tail_skips_bad_lines_without_stopping(tmp_path):
    f = tmp_path / "robot.log"
    f.write_text("", encoding="utf-8")               # 文件须先存在（tail -f 语义）
    src = FileTailSource(str(f), tenant_id="t1", device_sn="dev-1")
    src.poll(0.0)                                    # 打开、定位到末尾（空文件）
    with open(f, "a", encoding="utf-8") as fh:
        fh.write("not json\n")                       # 坏行
        fh.write(_raw_line("ok1") + "\n")            # 好行
        fh.write('{"valid":"json but not telemetry"}\n')  # 缺必需字段 → 交网关拒
    msgs = src.poll(0.0)
    # 非法 JSON 整行跳过；合法 JSON（即便缺字段）都进来，交给网关做字段校验。
    # 顺序：[ok1（好行）, None（合法 JSON 但无 event_id）]
    assert [m.payload.get("event_id") for m in msgs] == ["ok1", None]


def test_file_tail_waits_for_complete_line(tmp_path):
    f = tmp_path / "robot.log"
    f.write_text("", encoding="utf-8")
    src = FileTailSource(str(f), tenant_id="t1", device_sn="dev-1")
    src.poll(0.0)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write('{"event_id":"part')                  # 半行，无换行
    assert src.poll(0.0) == []                         # 半行不产出
    with open(f, "a", encoding="utf-8") as fh:
        fh.write('ial"}\n')                            # 补全
    msgs = src.poll(0.0)
    assert len(msgs) == 1 and msgs[0].payload["event_id"] == "partial"


def test_file_tail_handles_truncation(tmp_path):
    f = tmp_path / "robot.log"
    f.write_text("", encoding="utf-8")
    src = FileTailSource(str(f), tenant_id="t1", device_sn="dev-1")
    src.poll(0.0)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(_raw_line("e1") + "\n")
    assert len(src.poll(0.0)) == 1
    f.write_text("")                                   # 截断（logrotate 场景）
    src.poll(0.0)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(_raw_line("e2") + "\n")
    msgs = src.poll(0.0)
    assert len(msgs) == 1 and msgs[0].payload["event_id"] == "e2"


def test_file_tail_device_from_field(tmp_path):
    f = tmp_path / "robot.log"
    f.write_text("", encoding="utf-8")
    src = FileTailSource(str(f), tenant_id="t1", device_from_field="device_sn")
    src.poll(0.0)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(_raw_line("e1", device_field="SN-42") + "\n")
    msgs = src.poll(0.0)
    assert msgs[0].auth["device_sn"] == "SN-42"        # 从正文取设备号


def test_resolve_watch_path_picks_latest_in_dir(tmp_path):
    (tmp_path / "robot-1.log").write_text("a")
    time.sleep(0.01)
    (tmp_path / "robot-2.log").write_text("b")         # 更新
    assert Path(resolve_watch_path(str(tmp_path))).name == "robot-2.log"


# ---------------------------------------------------------------- HTTP 源

def test_http_push_source_ingests(tmp_path):
    from data_dog.stream.http import HttpPushSource
    src = HttpPushSource(tenant_id="cfg-tenant", host="127.0.0.1", port=0)
    host, port = src.address
    body = _raw_line("h1").encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/ingest", data=body,
        headers={"Content-Type": "application/json", "X-Device-Sn": "dev-9"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 202
        msgs = src.poll(1.0)
        assert len(msgs) == 1
        # 身份来自请求头；tenant 用配置（头里没给）
        assert msgs[0].auth["device_sn"] == "dev-9"
        assert msgs[0].auth["tenant_id"] == "cfg-tenant"
    finally:
        src.close()


def test_http_push_rejects_bad_json_and_wrong_path():
    from data_dog.stream.http import HttpPushSource
    src = HttpPushSource(tenant_id="t", host="127.0.0.1", port=0)
    host, port = src.address
    try:
        with pytest.raises(urllib.error.HTTPError) as e1:
            urllib.request.urlopen(urllib.request.Request(
                f"http://{host}:{port}/wrong", data=b"{}"), timeout=5)
        assert e1.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as e2:
            urllib.request.urlopen(urllib.request.Request(
                f"http://{host}:{port}/ingest", data=b"not json"), timeout=5)
        assert e2.value.code == 400
    finally:
        src.close()


# ---------------------------------------------------------------- runner

def _scripted_source(messages):
    """造一个只吐固定消息的源（测试 runner 用）。"""
    from data_dog.stream.base import StreamSource

    class _Src(StreamSource):
        name = "script"

        def __init__(self):
            self._msgs = list(messages)

        def poll(self, timeout):
            out, self._msgs = self._msgs, []
            return out

    return _Src()


def test_runner_groups_flush_by_tenant(plane_factory):
    """两个租户的消息各自落库到自己的租户（不串台）。"""
    persisted: list[tuple[str, int, int]] = []

    def persist(tenant, norms, cands):
        persisted.append((tenant, len(norms), len(cands)))

    msgs = [
        StreamMessage(payload=json.loads(_raw_line("a")),
                      auth={"tenant_id": "tA", "device_sn": "dA"}),
        StreamMessage(payload=json.loads(_raw_line("b")),
                      auth={"tenant_id": "tB", "device_sn": "dB"}),
    ]
    runner = StreamRunner(plane_factory(), _scripted_source(msgs),
                          persist=persist)
    stop = threading.Event()
    runner.run(stop=stop, poll_timeout=0.0, max_iters=2)

    tenants = {p[0] for p in persisted}
    assert tenants == {"tA", "tB"}, f"未按租户分组落库：{persisted}"
    assert runner.stats["messages"] == 2
    assert runner.stats["candidates"] >= 2      # 两租户各产出候选


def test_runner_isolates_tenants_in_candidate_ids(plane_factory):
    """**跨租户不泄漏**：同名设备号、不同租户 → 不同 candidate_id。"""
    seen: list = []

    def persist(tenant, norms, cands):
        seen.extend((tenant, c.candidate_id) for c in cands)

    msgs = [
        StreamMessage(payload=json.loads(_raw_line("a")),
                      auth={"tenant_id": "tA", "device_sn": "SAME"}),
        StreamMessage(payload=json.loads(_raw_line("b")),
                      auth={"tenant_id": "tB", "device_sn": "SAME"}),
    ]
    runner = StreamRunner(plane_factory(), _scripted_source(msgs),
                          persist=persist)
    runner.run(stop=threading.Event(), poll_timeout=0.0, max_iters=2)

    ids_a = {cid for t, cid in seen if t == "tA"}
    ids_b = {cid for t, cid in seen if t == "tB"}
    assert ids_a and ids_b
    assert ids_a.isdisjoint(ids_b), "同名设备跨租户候选 id 相撞（隔离漏洞）"


def test_runner_counts_rejected_and_flushes_when_idle(plane_factory):
    """被网关拒绝的消息要**计数**（不静默丢）；且源安静时也要 flush。

    缺 device_sn 的 auth → 网关失败关闭。若被拒却不计数，现象是"消息收到了、
    候选却是 0"，无从排查。
    """
    persisted: list = []
    runner = StreamRunner(
        plane_factory(),
        _scripted_source([StreamMessage(payload=json.loads(_raw_line("x")),
                                        auth={"tenant_id": "t"})]),  # 无 device
        persist=lambda tenant, norms, cands: persisted.append((tenant, norms)))
    # 第一轮拿到消息（暂存），第二轮源空 → 应触发 flush
    runner.run(stop=threading.Event(), poll_timeout=0.0, max_iters=2)
    assert runner.stats["rejected"] == 1
    assert runner.stats["normalized"] == 0
    assert runner.stats["flushes"] == 1          # 空闲触发落库，未卡在暂存区
    assert len(persisted) == 1                    # 即便无候选也落了库（空集）


def test_runner_survives_poll_error():
    from data_dog.stream.base import StreamSource

    class _Flaky(StreamSource):
        name = "flaky"

        def __init__(self):
            self.n = 0

        def poll(self, timeout):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("源掉线")
            return []

    runner = StreamRunner(None, _Flaky(), persist=lambda *a: None)
    runner.run(stop=threading.Event(), poll_timeout=0.0, max_iters=3)
    assert runner.stats["poll_errors"] == 1     # 掉线被记数，未中断循环


# ---------------------------------------------------------------- 工厂

def test_factory_builds_file_source(monkeypatch, tmp_path):
    f = tmp_path / "r.log"
    f.write_text("")
    monkeypatch.setenv("DD_STREAM_SOURCE", "file")
    monkeypatch.setenv("DD_STREAM_FILE_PATH", str(f))
    monkeypatch.setenv("DD_STREAM_TENANT", "tX")
    src = build_source()
    assert isinstance(src, FileTailSource) and src._tenant == "tX"


def test_factory_unknown_source_errors(monkeypatch):
    monkeypatch.setenv("DD_STREAM_SOURCE", "carrier-pigeon")
    with pytest.raises(RuntimeError, match="未知流式源类型"):
        build_source()


def test_factory_mqtt_without_dep_is_clear_error(monkeypatch):
    """缺 paho 时的报错要**指出装什么**，而非 bare ImportError。"""
    if _has("paho"):
        pytest.skip("本机装了 paho，无法断言缺依赖路径")
    monkeypatch.setenv("DD_STREAM_SOURCE", "mqtt")
    with pytest.raises(RuntimeError, match="paho-mqtt"):
        build_source()


def test_factory_kafka_without_dep_is_clear_error(monkeypatch):
    if _has("kafka"):
        pytest.skip("本机装了 kafka，无法断言缺依赖路径")
    monkeypatch.setenv("DD_STREAM_SOURCE", "kafka")
    with pytest.raises(RuntimeError, match="kafka-python"):
        build_source()


def _has(mod: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(mod) is not None
