"""无损视觉通道（B，VISTA-1 式）：多模态消息 + 内容寻址帧存储 + 按需放大。

钉住四件事：
1. provider 只在带 images 时才产出"内容分片"（文本链路逐字节不变）。
2. 帧存储**内容寻址、无损、去重**；裁剪产出**新帧**且原帧不动。
3. 视觉工具只在"挂了帧存储且 Pillow 可用"时暴露；调帧要过可信上下文（防跨设备）。
4. AgentLoop 把图像作为**独立观察**注入，且不把 base64 塞进 tool 文本。

约定：每个 test 结尾 print("PASS <name>")。
"""
from __future__ import annotations

import io
import json

from data_dog.agent.loop import VISUAL_HARNESS_PROMPT, AgentLoop
from data_dog.evidence.visual import VisualFrameStore
from data_dog.providers.client import LLMChatMessage, _to_api
from data_dog.tools.investigation import InvestigationTools

COMP = "joint_motor.front_left_hip"


def _png(w=8, h=8, color=(255, 0, 0)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


class _BareStore:
    def query_telemetry(self, *a, **k):
        return None

    def query_vendor_knowledge(self, *a, **k):
        return []

    def query_neighbor_events(self, *a, **k):
        return []

    def query_recent_changes(self, *a, **k):
        return {"changes": []}


def test_provider_image_parts():
    m = LLMChatMessage(role="user", content="看图",
                       images=[{"mime_type": "image/png", "data_base64": "AAA"}])
    d = _to_api(m)
    assert isinstance(d["content"], list)
    assert d["content"][0] == {"type": "text", "text": "看图"}
    assert d["content"][1]["image_url"]["url"] == "data:image/png;base64,AAA"
    d2 = _to_api(LLMChatMessage(role="user", content="hi"))
    assert d2["content"] == "hi"          # 纯文本链路不变
    d3 = _to_api(LLMChatMessage(role="tool", tool_call_id="t", content="{\"ok\":true}"))
    assert d3["role"] == "tool" and d3["content"] == '{"ok":true}'
    print("PASS test_provider_image_parts")


def test_store_is_content_addressed_and_lossless(tmp_path):
    st = VisualFrameStore(tmp_path / "vf")
    assert st.available is True
    raw = _png()
    a = st.put(raw, device_sn="d1", component_ref=COMP,
               observed_at="2026-10-12T00:00:00Z", label="cam0")
    b = st.put(_png(), device_sn="d1", component_ref=COMP)
    assert a["frame_id"] == b["frame_id"]                 # 同内容 → 同一帧
    assert len(list((tmp_path / "vf").glob("*.png"))) == 1
    c = st.put(_png(color=(0, 255, 0)), device_sn="d1", component_ref=COMP)
    assert c["frame_id"] != a["frame_id"]
    assert st.get_bytes(a["frame_id"]) == raw             # 原样取回，无重编码
    print("PASS test_store_is_content_addressed_and_lossless")


def test_store_crop_makes_new_frame(tmp_path):
    st = VisualFrameStore(tmp_path / "vf")
    a = st.put(_png(16, 16), device_sn="d1", component_ref=COMP,
               observed_at="2026-10-12T00:00:00Z")
    crop = st.crop(a["frame_id"], [1, 2, 9, 11])
    assert crop is not None
    assert crop["frame_id"] != a["frame_id"]
    assert crop["parent_frame_id"] == a["frame_id"]
    assert crop["crop_box"] == [1, 2, 9, 11]
    from PIL import Image
    with Image.open(io.BytesIO(st.get_bytes(crop["frame_id"]))) as im:
        assert im.size == (8, 9)
    assert st.crop(a["frame_id"], [9, 9, 1, 1]) is None    # 非法框 → None，不抛
    print("PASS test_store_crop_makes_new_frame")


def test_list_frames_filters(tmp_path):
    st = VisualFrameStore(tmp_path / "vf")
    st.put(_png(color=(1, 1, 1)), device_sn="d1", component_ref=COMP,
           observed_at="2026-10-12T00:00:00Z")
    st.put(_png(color=(2, 2, 2)), device_sn="d1", component_ref=COMP,
           observed_at="2026-10-12T05:00:00Z")
    st.put(_png(color=(3, 3, 3)), device_sn="d2", component_ref=COMP,
           observed_at="2026-10-12T02:00:00Z")
    got = st.list_frames(device_sn="d1", component_ref=COMP,
                         window_start="2026-10-12T00:00:00Z",
                         window_end="2026-10-12T03:00:00Z")
    assert len(got) == 1 and got[0]["observed_at"] == "2026-10-12T00:00:00Z"
    print("PASS test_list_frames_filters")


def test_visual_tools_gated_and_authorized(tmp_path, monkeypatch):
    monkeypatch.setenv("DD_LLM_VISION", "1")   # 本节验“能看图时”的行为
    t_off = InvestigationTools(_BareStore())
    assert "view_visual_frame" not in {s["name"] for s in t_off.specs()}
    vs = VisualFrameStore(tmp_path / "vf")
    t_on = InvestigationTools(_BareStore(), visual=vs)
    names = {s["name"] for s in t_on.specs()}
    assert {"query_visual_evidence", "view_visual_frame"} <= names
    fr = vs.put(_png(), device_sn="d1", component_ref=COMP, observed_at="x")
    tools = InvestigationTools(_BareStore(), visual=vs).with_task("d1")
    out = tools.call("view_visual_frame", {"frame_id": fr["frame_id"]})
    assert out["ok"] is True and out["__images__"][0]["mime_type"] == "image/png"
    other = InvestigationTools(_BareStore(), visual=vs).with_task("d2")
    out2 = other.call("view_visual_frame", {"frame_id": fr["frame_id"]})
    assert out2["ok"] is False and out2["error"] == "denied"
    print("PASS test_visual_tools_gated_and_authorized")


class _VisionProvider:
    """第一次返回 view_visual_frame 的 tool_call，第二次给结论；记录每次的消息。"""

    def __init__(self):
        self.calls: list[list] = []

    def chat(self, messages, tools=None, model=None, temperature=0.2, max_tokens=2048):
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            return {"kind": "tool_calls",
                    "tool_calls": [{"id": "s1", "name": "view_visual_frame",
                                    "arguments": json.dumps({"frame_id": self.frame_id})}],
                    "content": "", "__raw_tool_calls": []}
        return {"kind": "text", "content": json.dumps(
            {"disposition": "rejected", "event_type": "none", "severity": "info",
             "confidence": 0.9, "summary": "正常", "hypotheses": [],
             "missing_evidence": [], "recommended_actions": []})}


def test_agent_injects_image_not_base64(tmp_path, monkeypatch):
    monkeypatch.setenv("DD_LLM_VISION", "1")   # 本节验“能看图时”的行为
    from data_dog.schemas.events import CandidateEvent
    vs = VisualFrameStore(tmp_path / "vf")
    fr = vs.put(_png(), device_sn="d1", component_ref=COMP,
                observed_at="2026-10-12T00:00:00Z")
    prov = _VisionProvider()
    prov.frame_id = fr["frame_id"]
    trace: list = []
    cand = CandidateEvent(candidate_id="cand_x", device_sn="d1", component_ref=COMP,
                          signal="temperature_high", severity_hint="warning",
                          window_start="09:55", window_end="10:00", sample_count=1)
    AgentLoop(prov, InvestigationTools(_BareStore(), visual=vs),
              trace_cb=trace.append).investigate(cand)
    assert VISUAL_HARNESS_PROMPT in prov.calls[0][0].content
    second = prov.calls[1]
    img_msgs = [m for m in second if getattr(m, "images", None)]
    assert len(img_msgs) == 1                            # 图像作为独立观察注入
    tool_msgs = [m for m in second if m.role == "tool"]
    assert tool_msgs and "data_base64" not in tool_msgs[-1].content
    assert trace and "__images__" not in trace[0]["result"]   # 轨迹里不留 base64
    print("PASS test_agent_injects_image_not_base64")


def test_default_is_text_only_no_images(tmp_path, monkeypatch):
    """默认（未声明 DD_LLM_VISION）必须**不发图**，且把原因告诉人。

    这个用例守的是一个真实失败模式：配了纯文本模型（如 deepseek-chat）时，
    旧代码会无条件把 base64 原帧拼进 content parts，上游直接 400，
    整条调查失败关闭——“没配多模态”被放大成“分析不了”。
    降级后应是“看不到图，但分析照做”。
    """
    monkeypatch.delenv("DD_LLM_VISION", raising=False)
    vs = VisualFrameStore(tmp_path / "vf")
    fr = vs.put(_png(), device_sn="d1", component_ref=COMP,
                observed_at="2026-10-12T00:00:00Z")
    tools = InvestigationTools(_BareStore(), visual=vs).with_task("d1")
    out = tools.call("view_visual_frame", {"frame_id": fr["frame_id"]})
    assert out["ok"] is True
    assert "__images__" not in out, "未声明视觉能力却仍把原帧送去入模"
    assert "DD_LLM_VISION" in out.get("note", ""), "降级了但没说原因"
    print("PASS test_default_is_text_only_no_images")


def test_agent_text_only_does_not_inject_image_message(tmp_path, monkeypatch):
    """纯文本模型下，Agent 上下文里不应出现任何带图消息。"""
    monkeypatch.delenv("DD_LLM_VISION", raising=False)
    from data_dog.schemas.events import CandidateEvent
    vs = VisualFrameStore(tmp_path / "vf")
    fr = vs.put(_png(), device_sn="d1", component_ref=COMP,
                observed_at="2026-10-12T00:00:00Z")
    prov = _VisionProvider()
    prov.frame_id = fr["frame_id"]
    cand = CandidateEvent(candidate_id="cand_y", device_sn="d1", component_ref=COMP,
                          signal="temperature_high", severity_hint="warning",
                          window_start="09:55", window_end="10:00", sample_count=1)
    AgentLoop(prov, InvestigationTools(_BareStore(), visual=vs)).investigate(cand)
    for call in prov.calls:
        assert not [m for m in call if getattr(m, "images", None)], \
            "纯文本模型收到了带图消息（上游会 400 并让调查失败关闭）"
    print("PASS test_agent_text_only_does_not_inject_image_message")
