"""生成假 Go2 遥测日志（真机未到前，先把接入→候选这条管道跑通）。

每行一条 JSON，格式与 `data_plane/gateway.py` 的必需字段 + `p2_demo` 的电机载荷一致：
  - 7 个必需字段：event_id / occurred_at / received_at / sequence / vendor / model / firmware
  - protocol_version / component_hint：让服务端 demo 算子认得（兼容范围）
  - payload_inline：JSON 字符串，含电机指标 temperature/current/mode/dq/tau_est/lost/q

用法：
    python scripts/make_fake_go2_log.py --lines 20 --out D:/tmp/go2.log
    python scripts/make_fake_go2_log.py --lines 20 --out D:/tmp/go2.log --follow

--follow：写完不退出，每 0.5s 追加一条（模拟真机持续吐数据）。
          **配合 file 源使用**：file 源默认从文件末尾读，所以先起服务、再跑本脚本。

仅标准库，不依赖项目代码，可直接跑。
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

VENDOR = "unitree"
MODEL = "go2"
FIRMWARE = "3.2.1"                       # 必须在兼容范围内（9.9.9 会被失败关闭）
PROTOCOL = "motor-log-v2"                # 同理（old-v1 会被失败关闭）
COMPONENT = "joint_motor.front_left_hip"  # demo 算子盯的部件


def _iso(sec_offset: float) -> str:
    t = datetime(2026, 10, 10, 9, 55, 0, tzinfo=timezone.utc) + timedelta(seconds=sec_offset)
    return t.isoformat().replace("+00:00", "Z")


def make_event(seq: int) -> dict:
    """造第 seq 条：温度从 40°C 爬到 100°C+，越过 90°C 保护阈值 → 触发候选。"""
    temp = 40.0 + max(0, seq - 3) * 5.0          # 前几条正常，之后升温：seq≥13 越 90°C
    current = 6.0 + max(0, seq - 3) * 1.2
    payload = {
        "mode": 0,
        "temperature": round(temp, 1),
        "current": round(current, 2),
        "dq": round(0.1 * seq, 3),
        "tau_est": round(0.5 + 0.05 * seq, 3),
        "lost": 0,
        "q": round(0.02 * seq, 4),
    }
    return {
        "event_id": f"fake-{seq:05d}",
        "occurred_at": _iso(seq),
        "received_at": _iso(seq + 0.1),
        "sequence": seq,
        "vendor": VENDOR,
        "model": MODEL,
        "firmware": FIRMWARE,
        "protocol_version": PROTOCOL,
        "component_hint": COMPONENT,
        "payload_inline": json.dumps(payload, ensure_ascii=False),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", type=int, default=20, help="共生成多少条")
    ap.add_argument("--out", required=True, help="输出文件路径")
    ap.add_argument("--follow", action="store_true",
                    help="写完继续每 0.5s 追加（模拟真机持续吐数据）")
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("a", encoding="utf-8") as f:      # 追加：配合 file 源"先起服务再追加"
        for seq in range(1, args.lines + 1):
            f.write(json.dumps(make_event(seq), ensure_ascii=False) + "\n")
            f.flush()
        print(f"已写入 {args.lines} 条 → {out}")

        if args.follow:
            print("--follow：每 0.5s 追加一条，Ctrl+C 停止")
            seq = args.lines
            try:
                while True:
                    time.sleep(0.5)
                    seq += 1
                    f.write(json.dumps(make_event(seq), ensure_ascii=False) + "\n")
                    f.flush()
            except KeyboardInterrupt:
                print(f"\n停止，共写入 {seq} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
