"""厂商文档**用户导入**：切片 + 词法检索（纯逻辑，无外部依赖）。

缺口
----
A7 的真 RAG（chroma 向量库）走的是**项目内置**的公开宇树语料——全局一份，
用户无法导入**自己的**厂商文档。本模块补上"用户自带文档"这条通道：
上传文本 → 切片 → 落库（**租户隔离**）→ 检索。

诚实边界（关键）
----------------
- 这里的检索是**词法检索**（关键词命中 + 排序），**不是**向量语义检索。
  与 `knowledge.py` 的 chroma 向量 RAG 是**互补**关系，不是替代：词法检索确定、
  可离线测、零依赖；语义检索能跨同义词召回但要 embedding 服务。**不冒充**后者。
- 不做 PDF/DOCX 解析：入参是**已抽取的纯文本**。文档读取/解析（MarkItDown 等）
  是上游的事——本项目里对内是脚本、对外由调用方负责，本模块只管切片与检索。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

# 单块目标长度（字符）。与 scripts/chroma_ingest.py 的 MAX_CHUNK 对齐，
# 保证"用户导入"与"内置语料"的切片粒度一致，检索体验不因来源而分裂。
MAX_CHUNK = 700

# 词法检索的切词：连续 ASCII 词（字母/数字/下划线，长度≥2）或单个 CJK 字。
# CJK 无空格分词，退化为**按字匹配**（对一个字一个字查）——简单、确定，
# 不引入分词库依赖。文档内命中若干查询词即打分。
_WORD = re.compile(r"[A-Za-z0-9_]{2,}")
_CJK = re.compile(r"[一-鿿]")


def make_document_id(content_sha256: str) -> str:
    """文档 id：内容寻址（同内容同 id），导入幂等。"""
    return "kdoc_" + content_sha256[:12]


def make_chunk_id(document_id: str, ordinal: int) -> str:
    """块 id：文档 id + 序号派生（同文档同序号稳定）。"""
    h = hashlib.sha256(f"{document_id}#{ordinal}".encode()).hexdigest()
    return "kchunk_" + h[:12]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Chunk:
    ordinal: int
    chunk_id: str
    text: str


def chunk_text(text: str, *, document_id: str, max_chunk: int = MAX_CHUNK) -> list[Chunk]:
    """按空行分段，聚合成 ≤max_chunk 的块。

    与内置语料切片同款策略，但**不内嵌来源前缀**——来源信息是结构化列
    (`source_name`)，不再塞进文本正文（那里是给向量库的权宜之计）。
    """
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: list[Chunk] = []
    buf = ""
    for p in paras:
        if len(p) > max_chunk:              # 超长段按硬边界切
            for i in range(0, len(p), max_chunk):
                piece = p[i:i + max_chunk]
                if buf:
                    out.append(_mk(document_id, len(out), buf))
                    buf = ""
                out.append(_mk(document_id, len(out), piece))
            continue
        if buf and len(buf) + len(p) + 2 > max_chunk:
            out.append(_mk(document_id, len(out), buf))
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        out.append(_mk(document_id, len(out), buf))
    return out


def _mk(document_id: str, ordinal: int, text: str) -> Chunk:
    return Chunk(ordinal=ordinal, chunk_id=make_chunk_id(document_id, ordinal),
                 text=text)


def query_terms(query: str) -> list[str]:
    """把查询切成检索词：ASCII 词（小写化）+ CJK 单字。去重、保序。

    过短的（单字符 ASCII）丢弃——噪音大、几乎命中一切。
    """
    terms: list[str] = []
    seen: set[str] = set()
    for m in _WORD.finditer(query):
        t = m.group(0).lower()
        if t not in seen:
            seen.add(t)
            terms.append(t)
    for m in _CJK.finditer(query):
        t = m.group(0)
        if t not in seen:
            seen.add(t)
            terms.append(t)
    return terms


def score_chunk(text: str, terms: list[str]) -> float:
    """词法打分：命中的**不同**查询词占比（0..1）。命中越多分越高。

    用"不同词占比"而非总频次：避免一个高频词刷分淹没其它词的相关性。
    """
    if not terms:
        return 0.0
    low = text.lower()
    hits = sum(1 for t in terms if t in low)
    return hits / len(terms)


def rank_chunks(chunks: list[dict[str, Any]], query: str,
                *, min_score: float = 0.0) -> list[dict[str, Any]]:
    """对候选块按词法得分排序，附 `score` 字段；过滤掉低于 min_score 的。

    入参 chunks：每项含 `text`（必需）及任意透传字段（chunk_id/document_id…）。
    """
    terms = query_terms(query)
    scored: list[dict[str, Any]] = []
    for c in chunks:
        s = score_chunk(str(c.get("text", "")), terms)
        if s > min_score:
            scored.append({**c, "score": round(s, 4)})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored
