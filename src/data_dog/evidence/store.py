# -*- coding: utf-8 -*-
"""Mock 证据库：给调查 Agent 查询的受控证据。

生产环境里，这些数据来自真实的遥测时间序库、厂商知识库和事件流；
这里用一组写死的、自洽的假数据把「调查链路」跑通。
所有查询只读，Agent 通过受控 Tool 访问，拿不到全量原始日志。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..errors import ToolErrorCode, err
from .detectors import load_detector

# 可查询的部件范围白名单（模拟权限控制）
ALLOWED_COMPONENTS = {
    "joint_motor.front_left_hip",
    "joint_motor.front_right_hip",
    "joint_motor.front_left_knee",
    "battery.main",
    "controller.main",
}

# ---- 同设备、同部件在不同时间窗的遥测 ----#
# 结构：component -> window_key -> metrics dict
TELEMETRY: Dict[str, Dict[str, Dict[str, float]]] = {
    "joint_motor.front_left_hip": {
        # 一整段真实的升温过程（现场感）：09:40 起温度/电流随负载逐步爬升，
        # 到 09:50 已达 58°C（稳态偏热），09:55~10:00 突破 75°C 告警阈。
        # 保留 09:50-09:55 = 58.0 作为 09:55 主窗口的"上一窗参照"（不回退基线）。
        "09:40-09:45": {"temperature_c": 52.0, "current_a": 6.3, "velocity_rpm": 1265},
        "09:45-09:50": {"temperature_c": 55.0, "current_a": 6.1, "velocity_rpm": 1270},
        "09:50-09:55": {"temperature_c": 58.0, "current_a": 6.0, "velocity_rpm": 1280},
        # 主窗口：温度一路爬升到 78.2，电流也涨到 9.7 —— 异常信号
        "09:55-10:00": {
            "temperature_c": 78.2,
            "current_a": 9.7,
            "velocity_rpm": 1320,
        },
    },
    "joint_motor.front_right_hip": {
        "09:55-10:00": {"temperature_c": 61.0, "current_a": 6.2, "velocity_rpm": 1300},
    },
    "joint_motor.front_left_knee": {
        "09:55-10:00": {"temperature_c": 60.5, "current_a": 6.1, "velocity_rpm": 1290},
    },
    "battery.main": {
        "09:55-10:00": {"voltage_v": 51.2, "current_a": 18.5, "state_of_charge": 0.72},
    },
    "controller.main": {
        "09:55-10:00": {"cpu_temp_c": 52.0, "load": 0.4},
    },
}

# ---- 厂商知识条目（阈值、错误码、建议）----#
VENDOR_KNOWLEDGE: Dict[str, List[Dict[str, str]]] = {
    "joint_motor": [
        {
            "id": "KD-01",
            "domain": "temperature",
            "rule": "连续窗口温度 > 75°C 且当前载相同工况下电流高于基线 30% 以上，判定为热退化风险。",
            "source": "vendor-doc://motor-x2/thermal.md#L120",
        },
        {
            "id": "KD-02",
            "domain": "temperature",
            "rule": "电机温度上限 90°C，超过触发硬件过温保护并停机，属严重安全事件。",
            "source": "vendor-doc://motor-x2/safety.md#L45",
        },
        {
            "id": "KD-03",
            "domain": "cause",
            "rule": "电流持续偏高 + 温度爬升，可能原因：轴承摩擦增大 / 冷却通道退化 / 堵转。",
            "source": "vendor-doc://motor-x2/troubleshooting.md#L88",
        },
        {
            "id": "KD-04",
            "domain": "baseline",
            "rule": "该型号在空载/正常负载工况下，关节电机稳态电流参考基线 6.0~6.5 A。",
            "source": "vendor-doc://motor-x2/load-characteristics.md#L30",
        },
    ],
    "battery": [
        {
            "id": "KD-10",
            "domain": "voltage",
            "rule": "整机工作电压平台 48V，单节电芯低压保护阈值 3.0V。",
            "source": "vendor-doc://battery/spec.md#L12",
        },
    ],
}

# ---- 同设备其他部件的"邻居事件"（用于关联）----#
NEIGHBOR_EVENTS: Dict[str, List[Dict[str, Any]]] = {
    "joint_motor.front_left_hip": [
        {
            "event": "temp_rising",
            "component": "joint_motor.front_left_hip",
            "window": "09:55-10:00",
            "severity_hint": "warning",
        }
    ],
}

# ---- 近期变更（固件/算子/配置），供 Agent 调查时排查"变更新引入的异常" ----#
RECENT_CHANGES: Dict[str, List[Dict[str, Any]]] = {
    "joint_motor.front_left_hip": [
        {
            "type": "firmware",
            "id": "chg-101",
            "what": "关节电机固件升级 3.2.0 -> 3.2.1",
            "applied_at": "2026-08-12 18:00",
            "note": "变更冷却控制参数；相同工况电流基线可能波动",
            "relevance": "高",
        },
        {
            "type": "operator",
            "id": "chg-102",
            "what": "检测算子 motor-health-detector v0.3 -> v0.4",
            "applied_at": "2026-08-13 02:00",
            "note": "更新温度趋势规则 MOTOR_TEMP_TREND_V2，可能影响告警阈值",
            "relevance": "中",
        },
    ],
    "controller.main": [
        {
            "type": "config",
            "id": "chg-201",
            "what": "主控 PID 参数调整（负载响应）",
            "applied_at": "2026-08-13 08:00",
            "note": "可能改变电机驱动负载表现",
            "relevance": "中",
        }
    ],
    "battery.main": [],
}



class EvidenceStore:
    """只读的证据库，Agent 通过 Tool 访问。"""

    def __init__(self, detector: Any = None):
        # 桥接 A1：加载离线索生成的检测算子，供在线检测
        self._detector = detector if detector is not None else load_detector()

    def query_telemetry(
        self, device_sn: str, component: str, window_start: str, window_end: str
    ) -> Optional[Dict[str, Any]]:
        if component not in ALLOWED_COMPONENTS:
            return err(ToolErrorCode.DENIED, component)
        win = f"{window_start}-{window_end}"
        metrics = TELEMETRY.get(component, {}).get(win)
        if metrics is None:
            return None
        # 用生成的检测算子对这份遥测做在线检测，命中作为 Agent 证据
        detection = self._detector.detect(component, metrics)
        base = None
        comps = TELEMETRY.get(component, {})
        keys = sorted(comps.keys())
        if win in keys and keys.index(win) > 0:
            base = comps[keys[keys.index(win) - 1]]
        return {
            "device_sn": device_sn,
            "component": component,
            "window": win,
            "metrics": metrics,
            "reference_window_metrics": base,
            "detection": detection,   # 新增：生成算子的判定证据
        }

    def query_vendor_knowledge(
        self, component_type: str, question: Optional[str] = None
    ) -> List[Dict[str, str]]:
        # A7：DD_KB_CHROMA=1 → 走真 RAG（chroma-mcp 查询真实厂商语料）；
        # 失败则降级到内置条目并标注 retriever，不冒充 RAG 结果。
        import os
        if os.environ.get("DD_KB_CHROMA") == "1":
            try:
                from ..knowledge import query_knowledge
                hits = query_knowledge(question or component_type, n_results=3)
                if hits:
                    return [{
                        "id": h["id"],
                        "domain": "vendor-doc",
                        "rule": h["text"],
                        "source": h["source"],
                        "distance": h["distance"],
                        "retriever": "chroma-rag",
                    } for h in hits]
            except Exception:
                pass  # 降级到内置条目
        items = [{
            **item, "retriever": "builtin_dict"
        } for item in VENDOR_KNOWLEDGE.get(component_type, [])]
        # 简单按组件类型返回全部条目；生产会做语义检索
        return items

    def query_neighbor_events(
        self, device_sn: str, component: str, window_start: str, window_end: str
    ) -> List[Dict[str, Any]]:
        win = f"{window_start}-{window_end}"
        events = []
        for cmp, evs in NEIGHBOR_EVENTS.items():
            if cmp == component:
                for e in evs:
                    if e["window"] == win:
                        events.append(e)
        return events

    def query_recent_changes(
        self, device_sn: str, component: str, since: str
    ) -> List[Dict[str, Any]]:
        """查某部件近期固件/算子/配置变更（用于排查'变更新引入异常'）。"""
        # 存在部件即检查权限；变更表里无条目的部件返回空列表
        allowed = component in ALLOWED_COMPONENTS
        if not allowed:
            return err(ToolErrorCode.DENIED, component)
        return {"changes": RECENT_CHANGES.get(component, [])}
