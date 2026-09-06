# -*- coding: utf-8 -*-
"""Chroma MCP（RAG）冒烟测试：走 JSON-RPC 与 chroma-mcp 往返。

流程：initialize → 建集合 → 灌入厂商知识条目 → 语义查询验证召回。
用法：/d/.venv/Scripts/python.exe scripts/chroma_rag_smoke.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

CHROMA_EXE = r"D:\.venv-chroma\Scripts\chroma-mcp.exe"
DATA_DIR = r"D:\data_dog\.chroma"

# 与 data_dog/evidence/store.py 的 VENDOR_KNOWLEDGE 同源的条目
DOCS = [
    "连续窗口温度超过75摄氏度且电流高于基线30%以上，判定为电机热退化风险。",
    "电机温度上限90摄氏度，超过触发硬件过温保护并停机，属严重安全事件。",
    "电流持续偏高且温度爬升，可能原因：轴承摩擦增大、冷却通道退化、堵转。",
    "该型号电机在正常负载工况下，稳态电流参考基线为6.0至6.5安培。",
]
QUERY = "电机过温保护阈值是多少？什么时候会停机？"


class McpClient:
    """极简 MCP stdio 客户端：发请求、按 id 收响应。"""

    def __init__(self, cmd, env_extra=None):
        env = {**os.environ, **(env_extra or {})}
        self.p = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env,
        )
        self._next_id = 1

    def request(self, method, params=None, notify=False, timeout=600):
        msg = {"jsonrpc": "2.0", "method": method}
        if not notify:
            msg["id"] = self._next_id
            self._next_id += 1
        if params is not None:
            msg["params"] = params
        self.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        if notify:
            return None
        # 逐行读到对应 id 的响应
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("server closed stdout")
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            if resp.get("id") == msg["id"]:
                return resp

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()


def text_of(resp):
    """从 MCP tools/call 响应里取 text 内容。"""
    result = resp.get("result") or {}
    if result.get("isError"):
        return "ERROR: " + json.dumps(result, ensure_ascii=False)
    return "".join(c.get("text", "") for c in result.get("content", []))


def main():
    c = McpClient(
        [CHROMA_EXE, "--client-type", "persistent", "--data-dir", DATA_DIR],
        env_extra={"ANONYMIZED_TELEMETRY": "False"},
    )
    try:
        r = c.request("initialize", {"protocolVersion": "2024-11-05",
                                     "capabilities": {},
                                     "clientInfo": {"name": "smoke", "version": "0"}})
        print("[init]", r["result"]["serverInfo"])
        c.request("notifications/initialized", notify=True)

        r = c.request("tools/call", {"name": "chroma_create_collection",
                                     "arguments": {"collection_name": "vendor_knowledge"}})
        print("[create]", text_of(r)[:120])

        r = c.request("tools/call", {"name": "chroma_add_documents", "arguments": {
            "collection_name": "vendor_knowledge",
            "documents": DOCS,
            "ids": [f"kd-{i+1}" for i in range(len(DOCS))],
        }})
        print("[add]", text_of(r)[:200])

        r = c.request("tools/call", {"name": "chroma_query_documents", "arguments": {
            "collection_name": "vendor_knowledge",
            "query_texts": [QUERY],
            "n_results": 2,
        }}, timeout=600)
        print("[query] 问题:", QUERY)
        print(text_of(r))
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
