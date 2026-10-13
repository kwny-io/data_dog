"""机器人原始遥测 → data_dog 原始日志信封（接入真机唯一要你亲手写的转换器）。

为什么需要它
------------
机器人吐的字段名/单位/结构和 data_dog 要的不一样。本脚本做一次"翻译"：
    机器人原始行  →  map_row()  →  data_dog 要的 raw log  →  喂给流式源

你只需要改 `map_row()` 一个函数。

原始行长什么样（**示例，按你机器人的真实输出改**）
------------------------------------------------
假设通过 SDK/日志拿到类似这样的一行（宇树 Go2 关节状态）：
{
  "time": 1758000000.5,        # Unix 时间戳（秒）
  "sn": "G0225AABBCC",         # 设备序列号
  "joint": "FL_hip",           # 关节名（厂商叫法）
  "motor_temp": 78.5,          # 电机温度 °C
  "motor_current": 4.2,        # 电流 A
  "motor_mode": 0,
  "joint_q": 0.12,             # 关节角
  "joint_dq": 0.05,            # 关节速度
  "tau": 0.7                   # 估计力矩
}

data_dog 要的格式（见 docs/real_device_integration.md §3）
--------------------------------------------------------
7 个必需字段：event_id / occurred_at / received_at / sequence / vendor / model / firmware
+ component_hint（部件名，**必须映射到算子认的名字**）+ payload_inline（指标 JSON 字符串）

最关键的坑：component_hint 必须对上算子
---------------------------------------
demo 算子盯的是 `joint_motor.front_left_hip`（见 operator_factory/p1_demo.py）。
如果机器人叫 "FL_hip"，你必须映射成 `joint_motor.front_left_hip`——
**名字对不上，日志能进去，但候选永远出不来。**

用法
----
    python scripts/to_data_dog.py --in raw.jsonl --out /tmp/go2.log
每读一行原始 JSON，产出一行 data_dog 格式，追加到 --out。仅标准库。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

# ---- 这些按你的机器人/场景填 ----
VENDOR = "unitree"
MODEL = "go2"
FIRMWARE = "3.2.1"                       # 要在算子兼容范围内，否则失败关闭
PROTOCOL = "motor-log-v2"

# 厂商关节名 → 算子部件名。**这里是最容易翻车的地方，按真机补全。**
JOINT_TO_COMPONENT = {
    "FL_hip": "joint_motor.front_left_hip",
    "FR_hip": "joint_motor.front_right_hip",
    "RL_hip": "joint_motor.rear_left_hip",
    "RR_hip": "joint_motor.rear_right_hip",
}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def map_row(raw: dict, seq: int) -> dict:
    """★ 你要改的就是这个函数：原始行 → data_dog raw log。

    左边是 data_dog 要的字段（不改），右边从 raw 里取（按你机器人的字段名改）。
    """
    joint = str(raw.get("joint", ""))
    component = JOINT_TO_COMPONENT.get(joint, joint)   # 映射到算子认的部件名

    payload = {
        "mode": int(raw.get("motor_mode", 0)),
        "temperature": float(raw.get("motor_temp", 0.0)),
        "current": float(raw.get("motor_current", 0.0)),
        "q": float(raw.get("joint_q", 0.0)),
        "dq": float(raw.get("joint_dq", 0.0)),
        "tau_est": float(raw.get("tau", 0.0)),
        "lost": int(raw.get("lost", 0)),
    }

    ts = float(raw.get("time", 0.0)) or datetime.now(tz=timezone.utc).timestamp()
    return {
        "event_id": f"{raw.get('sn', 'dev')}-{seq:06d}",
        "occurred_at": _iso(ts),
        "received_at": _iso(datetime.now(tz=timezone.utc).timestamp()),
        "sequence": seq,
        "vendor": VENDOR,
        "model": MODEL,
        "firmware": FIRMWARE,
        "protocol_version": PROTOCOL,
        "component_hint": component,            # ← 务必对上算子部件名
        "payload_inline": json.dumps(payload, ensure_ascii=False),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="机器人原始行（每行一条 JSON）")
    ap.add_argument("--out", required=True, help="输出 data_dog 格式日志（追加）")
    args = ap.parse_args()

    src, dst = Path(args.inp), Path(args.out)
    dst.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with src.open("r", encoding="utf-8") as f_in, dst.open("a", encoding="utf-8") as f_out:
        for line in f_in:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue                        # 坏行跳过，不中断（对齐 file 源行为）
            n += 1
            f_out.write(json.dumps(map_row(raw, n), ensure_ascii=False) + "\n")
            f_out.flush()
    print(f"转换 {n} 条 → {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
