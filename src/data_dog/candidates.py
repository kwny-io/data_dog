"""候选测试用例清单 —— 供 data-dog test 交互窗选择。

每个用例选一个 **store.py 里真实有遥测数据** 的部件 + 一个描述与预期结论，
这样挑不同用例调查，Agent 会因数据不同给出不同结论（有对比，体现专业性）。

说明：当前遥测是 mock 剧本（见 evidence/store.py 的 TELEMETRY），
所以候选组件必须落在 store 认识的部件上；将来接真实设备后，
候选就来自实遥测流，本清单可作为"回归测试集"复用。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TestCase:
    key: str          # 唯一 id
    label: str        # 给用户看的名称
    component: str    # 查哪个部件（须有遥测）
    window: tuple     # (start, end)
    signal: str       # 候选异常信号名
    severity: str     # warning/critical
    note: str         # 这个案例想展示什么（预期结论）
    expect_hint: str  # 期望的裁决倾向（供展示/文档，非断言）


# 3 个有对比的用例：正常 / 温度故障 / 电池检查
TEST_CASES = [
    TestCase(
        key="case-normal-right-hip",
        label="右髋电机（正常数据）→ 应排除不误报",
        component="joint_motor.front_right_hip",
        window=("09:55", "10:00"),
        signal="spurious_alarm_check",
        severity="warning",
        note="温度61/电流6.2 在正常基线内，Agent 应判 rejected，证明不误报",
        expect_hint="rejected / inconclusive",
    ),
    TestCase(
        key="case-temp-hip",
        label="左髋电机（温度爬升）→ 热退化告警",
        component="joint_motor.front_left_hip",
        window=("09:55", "10:00"),
        signal="temperature_rising_under_normalized_load",
        severity="warning",
        note="温度78.2 超75阈值 → 热退化/需人工复核",
        expect_hint="needs_human_review / confirmed",
    ),
    TestCase(
        key="case-battery",
        label="电池（BMS 检查）→ 是否异常",
        component="battery.main",
        window=("09:55", "10:00"),
        signal="battery_state_check",
        severity="warning",
        note="SOC 0.72 正常（阈值0.2）→ 应正常，展示 BMS 也在巡检",
        expect_hint="rejected / needs_human_review",
    ),
]

BY_KEY = {c.key: c for c in TEST_CASES}


def make_candidate(case: TestCase):
    """把一个 TestCase 转成 CandidateEvent（复用现有 schema）。"""
    from .schemas.events import CandidateEvent
    return CandidateEvent(
        candidate_id=case.key,
        device_sn="device-abc123",
        component_ref=case.component,
        signal=case.signal,
        severity_hint=case.severity,
        window_start=case.window[0],
        window_end=case.window[1],
        sample_count=300,
        rule_matches=["TEST"],
        anomaly_score=0.5,
        detector_provenance="test-harness",
    )
