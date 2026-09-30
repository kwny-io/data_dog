"""事件时间 / 水位线 / 迟到事件测试（G2，对齐 gap 文档 §5 验收）。

覆盖：
  - 水位线单调 + is_late
  - WindowBuffer 乱序重排：slope/mean 与顺序到达一致
  - 合并从无界 abs 收紧为有向 + 水位线：容忍窗内并入、超容忍拒（新开组）
  - 确定性：同输入（任意到达顺序）→ 同候选集
  - 零回归：未注入 watermark 时退回旧 abs 语义
"""
from __future__ import annotations

from data_dog.data_plane.candidate import CandidateBuilder
from data_dog.data_plane.chain import WindowBuffer
from data_dog.data_plane.watermark import Watermark


def _hit():
    return [{"rule_id": "MOTOR_TEMP_PROTECT", "field": "temperature", "value": 95,
             "operator": "ge", "threshold": 90.0, "severity_hint": "warning",
             "operator_id": "d"}]


def _t(sec: int) -> str:
    return f"2026-09-13T10:00:{sec:02d}Z"


# ---------------------------------------------------------------- 水位线

def test_watermark_monotonic():
    w = Watermark(allowed_lateness_s=30.0)
    for s in (100.0, 90.0, 120.0, 110.0):     # 乱序喂入
        w.observe(s)
    assert w.max_seen == 120.0
    assert w.value == 90.0                    # 只增不减

    for s in (95.0, 80.0):
        w.observe(s)
    assert w.value == 90.0                     # 回退的 observe 不降低水位线


def test_watermark_is_late():
    w = Watermark(allowed_lateness_s=10.0)
    w.observe(100.0)
    assert w.value == 90.0
    assert w.is_late(89.0) is True
    assert w.is_late(90.0) is False
    assert w.is_late(95.0) is False


# ---------------------------------------------------------------- 窗口重排

def test_window_reorders_by_event_time():
    """乱序到达 → 窗口内容按事件时间有序 → slope/mean 与顺序到达一致。"""
    ordered = WindowBuffer(size=10)
    shuffled = WindowBuffer(size=10)
    pts = [(100.0, 1.0), (101.0, 2.0), (102.0, 3.0), (103.0, 4.0)]
    for t, v in pts:
        ordered.push("k", {"temperature": v}, occurred_at_s=t)
    for t, v in reversed(pts):
        shuffled.push("k", {"temperature": v}, occurred_at_s=t)

    fo = ordered.features("k")
    fs = shuffled.features("k")
    assert shuffled._samples["k"][0][2]["temperature"] == 1.0   # 最早在前
    assert fo.mean["temperature"] == fs.mean["temperature"]
    assert fo.slope["temperature"] == fs.slope["temperature"]
    assert fo.last["temperature"] == fs.last["temperature"]


def test_window_without_time_is_backward_compatible():
    """不给事件时间 → 按到达顺序（旧行为）。"""
    w = WindowBuffer(size=10)
    for v in (1, 2, 3, 4):
        w.push("k", {"temperature": v})
    f = w.features("k")
    assert f.mean["temperature"] == 2.5
    assert f.last["temperature"] == 4.0


def test_window_evicts_earliest_by_event_time():
    w = WindowBuffer(size=3)
    for t in (103.0, 101.0, 102.0, 100.0):     # 乱序，只留最近 3 个事件时间
        w.push("k", {"temperature": t}, occurred_at_s=t)
    kept = [e[0] for e in w._samples["k"]]
    assert kept == [101.0, 102.0, 103.0]       # 100 被淘汰


# ---------------------------------------------------------------- 有界迟到

def test_late_within_tolerance_merges():
    """容忍窗内的迟到事件 → 并入已有组（late_merged）。"""
    wm = Watermark(allowed_lateness_s=30.0)
    b = CandidateBuilder(merge_window_s=5.0, watermark=wm)
    b.add(device_sn="dev", component_ref="c", metrics={"temperature": 95},
          hits=_hit(), occurred_at=_t(10), received_at="t")     # t=10
    b.add(device_sn="dev", component_ref="c", metrics={"temperature": 96},
          hits=_hit(), occurred_at=_t(7), received_at="t")      # 迟到 3s（容忍内）
    cands = b.build()
    assert len(cands) == 1
    assert cands[0].sample_count == 2
    assert b.late_merged == 1
    assert b.late_rejected == 0


def test_too_late_does_not_merge():
    """太晚（早于水位线）→ 不与旧组混，新开组。"""
    wm = Watermark(allowed_lateness_s=5.0)
    b = CandidateBuilder(merge_window_s=5.0, watermark=wm)
    b.add(device_sn="dev", component_ref="c", metrics={"temperature": 95},
          hits=_hit(), occurred_at=_t(10), received_at="t")     # max_seen=10
    # 水位线 = 10-5 = 5；t=0 早于水位线 → 拒（新开组）
    b.add(device_sn="dev", component_ref="c", metrics={"temperature": 96},
          hits=_hit(), occurred_at="2026-09-13T09:59:58Z", received_at="t")
    cands = b.build()
    assert len(cands) == 2                     # 不混
    assert b.late_rejected == 1


def test_unbounded_abs_regression_when_no_watermark():
    """未注入 watermark → 退回旧无界 abs：1 小时前的事件也会并入（旧行为）。"""
    b = CandidateBuilder(merge_window_s=5.0)   # 无 watermark
    b.add(device_sn="dev", component_ref="c", metrics={"temperature": 95},
          hits=_hit(), occurred_at="2026-09-13T10:00:00Z", received_at="t")
    # abs(3600) <= 5? 否 → 其实 abs 也不会并（窗口 5s）。改用大窗口验证 abs 语义：
    b2 = CandidateBuilder(merge_window_s=7200.0)
    b2.add(device_sn="dev", component_ref="c", metrics={"temperature": 95},
           hits=_hit(), occurred_at="2026-09-13T10:00:00Z", received_at="t")
    b2.add(device_sn="dev", component_ref="c", metrics={"temperature": 96},
           hits=_hit(), occurred_at="2026-09-13T09:00:00Z", received_at="t")
    # 无水位线时 abs 只看距离（3600 <= 7200）→ 并入（这正是要收紧的行为）
    assert len(b2.build()) == 1


def test_deterministic_candidate_set_regardless_of_arrival_order():
    """同事件集（任意到达顺序）→ 同候选集 + 同 candidate_id。"""
    def run(order):
        wm = Watermark(allowed_lateness_s=30.0)
        b = CandidateBuilder(merge_window_s=5.0, watermark=wm)
        for sec in order:
            b.add(device_sn="dev", component_ref="c",
                  metrics={"temperature": 95}, hits=_hit(),
                  occurred_at=_t(sec), received_at="t")
        return [(c.candidate_id, c.sample_count) for c in b.build()]

    a = run([10, 11, 12, 13])
    b = run([13, 10, 12, 11])                  # 全部在容忍窗内 → 同一组
    assert a == b


def test_candidate_id_stable_after_late_merge():
    """并入迟到事件改变 sample_count，但 candidate_id 稳定（由组 key 派生）。"""
    wm = Watermark(allowed_lateness_s=30.0)
    b = CandidateBuilder(merge_window_s=5.0, watermark=wm)
    b.add(device_sn="dev", component_ref="c", metrics={"temperature": 95},
          hits=_hit(), occurred_at=_t(10), received_at="t")
    cid_before = b.build()[0].candidate_id
    b.add(device_sn="dev", component_ref="c", metrics={"temperature": 96},
          hits=_hit(), occurred_at=_t(8), received_at="t")     # 迟到并入
    c2 = b.build()[0]
    assert c2.candidate_id == cid_before
    assert c2.sample_count == 2


def test_window_end_takes_max_event_time():
    """乱序并入后 window_end 仍取最大事件时间（不被迟到事件拉回）。"""
    wm = Watermark(allowed_lateness_s=30.0)
    b = CandidateBuilder(merge_window_s=5.0, watermark=wm)
    b.add(device_sn="dev", component_ref="c", metrics={"temperature": 95},
          hits=_hit(), occurred_at=_t(10), received_at="t")
    b.add(device_sn="dev", component_ref="c", metrics={"temperature": 96},
          hits=_hit(), occurred_at=_t(8), received_at="t")     # 迟到
    c = b.build()[0]
    assert c.window_start == _t(10)
    assert c.window_end == _t(10)              # 不被 t=8 拉回
