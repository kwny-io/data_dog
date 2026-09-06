# -*- coding: utf-8 -*-
"""A7：把 vendor_docs/unitree 语料分块灌入 Chroma（经 chroma-mcp）。

用法：/d/.venv/Scripts/python.exe scripts/chroma_ingest.py
- 语料：vendor_docs/unitree/**/*.md + *.idl.txt（真实公开厂商资料）
- 分块：按空行分段，聚合到 ≤700 字符的 chunk（来源路径内嵌在文本首行）
- 集合：unitree_go2（create 幂等；不用 delete——chroma-mcp 0.2.6 对
  delete_collection 会崩 server，实测踩坑）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chroma_rag_smoke import CHROMA_EXE, McpClient, text_of  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = ROOT / "vendor_docs" / "unitree"
COLLECTION = "unitree_go2"
DATA_DIR = ROOT / ".chroma"
MAX_CHUNK = 700


def iter_corpus_files():
    for pattern in ("*.md", "*.idl.txt"):
        yield from sorted(CORPUS_DIR.rglob(pattern))


def chunk_text(text: str, source: str):
    """按空行分段 → 聚合成 ≤MAX_CHUNK 的 chunk。"""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if len(p) > MAX_CHUNK:  # 超长段按行再切
            p = p[:MAX_CHUNK]
        if buf and len(buf) + len(p) + 2 > MAX_CHUNK:
            chunks.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        chunks.append(buf)
    return [f"[来源: {source}]\n{c}" for c in chunks]


def main() -> int:
    docs, ids = [], []
    for f in iter_corpus_files():
        rel = str(f.relative_to(CORPUS_DIR))
        text = f.read_text(encoding="utf-8", errors="replace")
        for i, c in enumerate(chunk_text(text, rel)):
            ids.append(f"{rel}#{i}")
            docs.append(c)
    print(f"语料: {len(set(i.split('#')[0] for i in ids))} 个文件 → {len(docs)} 个 chunk", flush=True)
    if not docs:
        print("没有语料，退出")
        return 1

    c = McpClient(
        [CHROMA_EXE, "--client-type", "persistent", "--data-dir", str(DATA_DIR)],
        env_extra={"ANONYMIZED_TELEMETRY": "False"},
    )
    try:
        # MCP 握手：initialize → notifications/initialized（不发握手 server 会断连！）
        c.request("initialize", {"protocolVersion": "2024-11-05",
                                 "capabilities": {},
                                 "clientInfo": {"name": "ingest", "version": "0"}})
        c.request("notifications/initialized", notify=True)

        r = c.request("tools/call", {"name": "chroma_create_collection",
                                     "arguments": {"collection_name": COLLECTION}})
        txt = text_of(r)
        if (r.get("result") or {}).get("isError") is True and "already exists" not in txt:
            print("[create] 失败:", txt[:200])
            return 1
        print("[create]", ("已存在，复用" if "already exists" in txt else "新建"), flush=True)

        B = 16  # 分批灌入（小批更稳）
        for i in range(0, len(docs), B):
            r = c.request("tools/call", {"name": "chroma_add_documents", "arguments": {
                "collection_name": COLLECTION,
                "documents": docs[i:i+B], "ids": ids[i:i+B],
            }})
            txt = text_of(r)
            if (r.get("result") or {}).get("isError") is True:
                if "already exist" in txt:
                    print(f"[add {min(i+B, len(docs))}/{len(docs)}] skip（已存在，内容确定性相同）", flush=True)
                    continue
                print(f"[add {i}..] ERROR:", txt[:300])
                return 1
            print(f"[add {min(i+B, len(docs))}/{len(docs)}] ok", flush=True)
        print(f"✅ 集合 {COLLECTION} 就绪: {len(docs)} chunks")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
