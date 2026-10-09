"""RAG 质检：白名单抽检 + 人工复核（回填 10 文档的可复现脚本）。

用法（需已配置 DD_KB_CHROMA=1 时用 RAG）：
    /d/.venv/Scripts/python.exe scripts/rag_quality_check.py [--channels]

白名单抽检（机器自动判）：定义「查询 → 期望召回来源/字段」，检查 top-3 命中里
是否包含。这是"有没有召回过正确的东西"的标准做法（hit@k 的简化）。
人工复核（人看）：把命中原文打出来，判断"是不是知识本身"。

注意：当前不涉及 SQL；本项目证据在内存 mock + Chroma 向量库。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("DD_KB_CHROMA", "1")
from data_dog.knowledge import query_knowledge  # noqa: E402

# 白名单：每个查询「应该召回」的期望字串（来源文件名 + 关键字段）
WHITELIST = {
    "MotorState temperature 字段": ["MotorState_.idl.txt", "temperature"],
    "电池BMS soc和电流": ["BmsState_.idl.txt", "soc", "current"],
    "IMU 姿态": ["IMUState_.idl.txt"],
}


def whitelist_check() -> int:
    ok_all = True
    for q, expect in WHITELIST.items():
        hits = query_knowledge(q, n_results=3)
        joined = " ".join(h["source"] for h in hits) + " " + \
                 " ".join(h["text"] for h in hits)
        ok = all(e in joined for e in expect)
        ok_all &= ok
        print(("✓" if ok else "✗"), q, "→", f"top{len(hits)}命中")
        if not ok:
            print("   缺失期望字串:", [e for e in expect if e not in joined])
    print("白名单抽检全过" if ok_all else "有召回失败")
    return 0 if ok_all else 1


def manual_review() -> None:
    print("\n=== 人工复核（人眼看是不是真知识）===")
    for q in ["电机过温会怎样", "电池BMS soc和电流"]:
        print(f"\n-- 查询: {q}")
        for h in query_knowledge(q, n_results=3):
            print(f"  [来源] {h['source']}  [距离] {round(h['distance'] or 0, 3)}")
            print(f"  [文本] {h['text'][:220].strip()}")
            print()


if __name__ == "__main__":
    code = whitelist_check()
    manual_review()
    sys.exit(code)
