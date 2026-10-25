"""给 `tools/verify_compose_web.sh` 用的断言：候选响应里必须真有本次投递的设备。

为什么单独一个文件而不是 heredoc：heredoc 里的失败**不会**自动传播成脚本退出码
（2026-10-25 就是这样打印出了假绿）；独立进程的退出码能被 `set -u` 的脚本直接判断。
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    # Windows 控制台默认 GBK，直接 print(✓/✗) 会 UnicodeEncodeError 把断言崩掉
    # （2026-10-25 实际发生：错误不是“没命中”，而是“打印不出来”）。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:                                             # noqa: BLE001
        pass
    if len(argv) != 3:
        print("用法: _assert_candidate.py <候选响应.json> <device_sn>", file=sys.stderr)
        return 2
    path, sn = argv[1], argv[2]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:                                    # noqa: BLE001
        print(f"✗ 读不到/解析不了候选响应 {path}：{type(e).__name__}: {e}")
        return 1

    items = data.get("items") or []
    print(f"  候选条数={len(items)} total={data.get('total')} count={data.get('count')}")
    hit = [i for i in items if str(i.get("device_sn")) == sn]
    if not hit:
        print(f"✗ 没有 device_sn={sn} 的候选；实际 device_sn={[i.get('device_sn') for i in items]}")
        return 1
    c = hit[0]
    print(f"  ✓ 命中 {c.get('candidate_id')} signal={c.get('signal')} "
          f"severity={c.get('severity_hint')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
