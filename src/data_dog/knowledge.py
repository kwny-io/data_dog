# -*- coding: utf-8 -*-
"""A7：RAG 知识检索 —— 经 chroma-mcp 查询真实厂商知识库。

连接方式与 mcp_context.py 同款：常驻 stdio MCP 客户端（一次 spawn 多次查询，
进程死亡自动重建）。查询集合由 DD_KB_COLLECTION 控制（默认 unitree_go2，
语料 = vendor_docs/unitree 下的真实公开厂商资料）。

失败语义：查询失败向上抛 McpContextError 同款异常；调用方（EvidenceStore）
决定降级策略——知识查询属只读操作，降级到内置条目并标注来源，不冒充 RAG 结果。
"""
from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_CHROMA_EXE = Path(r"D:\.venv-chroma\Scripts\chroma-mcp.exe")
REQUEST_TIMEOUT_S = 60.0


class KnowledgeRetrievalError(RuntimeError):
    """RAG 检索失败（server 不可用 / 响应非法）。"""


def _chroma_exe() -> Path:
    override = os.environ.get("DD_CHROMA_MCP_EXE", "").strip()
    return Path(override) if override else DEFAULT_CHROMA_EXE


def _collection() -> str:
    return os.environ.get("DD_KB_COLLECTION", "unitree_go2")


class ChromaKnowledgeClient:
    """与 chroma-mcp 的常驻 stdio 连接（persist 数据目录 = 项目 .chroma）。"""

    def __init__(self, exe: Path, data_dir: Path):
        self.exe = exe
        self.data_dir = data_dir
        self.proc: Optional[subprocess.Popen] = None
        self._next_id = 1

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def close(self) -> None:
        """强杀收尾：Windows 下优雅关闭经常留僵尸进程（实测踩坑），直接 kill。"""
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=1)
        except Exception:
            pass
        try:
            self.proc.kill()
        except Exception:
            pass
        self.proc = None

    def _kill(self) -> None:
        if self.proc is not None:
            try:
                self.proc.kill()
            except Exception:
                pass

    def _start(self) -> None:
        if not self.exe.exists():
            raise KnowledgeRetrievalError(f"chroma-mcp 不存在: {self.exe}")
        self.proc = subprocess.Popen(
            [str(self.exe), "--client-type", "persistent",
             "--data-dir", str(self.data_dir)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8",
            env={**os.environ, "ANONYMIZED_TELEMETRY": "False"},
        )
        r = self._request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "data_dog-kb", "version": "0.1.0"},
        })
        name = (r.get("serverInfo") or {}).get("name", "")
        if name != "chroma":
            raise KnowledgeRetrievalError(f"非预期的 server: {name!r}")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # ---- JSON-RPC 基元（与 mcp_context 同构）----
    def _send(self, msg: Dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _recv(self, req_id: int) -> Dict[str, Any]:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise KnowledgeRetrievalError("chroma-mcp 无响应（提前退出/被超时终止）")
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            if resp.get("id") == req_id:
                if "error" in resp:
                    raise KnowledgeRetrievalError(f"JSON-RPC 错误: {resp['error']}")
                return resp["result"]

    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        req_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        watchdog = threading.Timer(REQUEST_TIMEOUT_S, self._kill)
        watchdog.daemon = True
        try:
            watchdog.start()
            return self._recv(req_id)
        finally:
            watchdog.cancel()

    # ---- 业务 ----
    def query(self, question: str, n_results: int = 3) -> List[Dict[str, Any]]:
        if not self.is_alive():
            self.close()
            self._start()
        # 注意：_request 已返回 JSON-RPC 的 result 部分，不要再取一层 .result
        result = self._request("tools/call", {"name": "chroma_query_documents",
                                              "arguments": {
            "collection_name": _collection(),
            "query_texts": [question],
            "n_results": n_results,
        }})
        if result.get("isError") is True:
            raise KnowledgeRetrievalError(f"查询失败: {result}")
        texts = [c.get("text", "") for c in result.get("content", [])]
        raw = "".join(texts)
        try:
            payload = json.loads(raw)  # chroma 查询结果 dict
        except json.JSONDecodeError:
            # 带上原文，避免吞掉真实失败原因（如 database is locked）
            raise KnowledgeRetrievalError(f"响应不是合法 JSON: {raw[:300]!r}") from None
        ids = payload.get("ids") or [[]]
        docs = payload.get("documents") or [[]]
        dists = payload.get("distances") or [[]]
        metas = payload.get("metadatas") or [[]]
        hits: List[Dict[str, Any]] = []
        for i, doc in enumerate(docs[0]):
            meta = (metas[0][i] if metas[0] and i < len(metas[0]) else {}) or {}
            # 来源优先取 metadata；ingest 未存 metadata 时从文本首行 [来源: x] 提取
            source = meta.get("source")
            if not source and doc.startswith("[来源: "):
                source = doc.split("]", 1)[0].removeprefix("[来源: ")
            hits.append({
                "id": ids[0][i] if i < len(ids[0]) else f"hit{i}",
                "text": doc,
                "source": source or "unknown",
                "distance": dists[0][i] if i < len(dists[0]) else None,
            })
        return hits


_PERSISTENT: Optional[ChromaKnowledgeClient] = None


def close_knowledge_server() -> None:
    global _PERSISTENT
    if _PERSISTENT is not None:
        _PERSISTENT.close()
        _PERSISTENT = None


atexit.register(close_knowledge_server)


def query_knowledge(question: str, n_results: int = 3) -> List[Dict[str, Any]]:
    """查一次 RAG 知识库；常驻复用，坏了自动重建重试一次。"""
    global _PERSISTENT
    client = _PERSISTENT
    from .paths import data_dir as _data_dir
    chroma_dir = _data_dir() / ".chroma"
    try:
        if client is None:
            client = ChromaKnowledgeClient(_chroma_exe(), chroma_dir)
        return client.query(question, n_results)
    except Exception:
        # 失败路径必须关掉旧进程（否则泄漏僵尸 server，锁库引发间歇性故障）
        if client is not None:
            client.close()
        client = ChromaKnowledgeClient(_chroma_exe(), chroma_dir)
        try:
            return client.query(question, n_results)
        finally:
            _PERSISTENT = client
