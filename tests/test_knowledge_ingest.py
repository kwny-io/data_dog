"""用户厂商文档导入的纯逻辑测试（`knowledge_ingest`）。

切片与词法检索都是**确定性**的：同输入必然同分块、同得分。重点守住：
  - 内容寻址：同内容 → 同 document_id（导入幂等的基础）；
  - 切片遵守长度上限、超长段被硬切；
  - 词法打分用"命中不同词占比"，一个高频词不能刷分淹没其它词；
  - CJK 无空格也能逐字命中（不依赖分词库）。
"""
from __future__ import annotations

from data_dog.knowledge_ingest import (
    MAX_CHUNK,
    chunk_text,
    make_chunk_id,
    make_document_id,
    query_terms,
    rank_chunks,
    score_chunk,
    sha256_text,
)

# ---------------------------------------------------------------- id 派生

def test_document_id_content_addressed():
    """同内容 → 同 document_id（导入幂等的基础）。"""
    a = make_document_id(sha256_text("hello"))
    b = make_document_id(sha256_text("hello"))
    c = make_document_id(sha256_text("world"))
    assert a == b and a != c
    assert a.startswith("kdoc_")


def test_chunk_id_stable_per_ordinal():
    did = make_document_id(sha256_text("x"))
    assert make_chunk_id(did, 0) == make_chunk_id(did, 0)
    assert make_chunk_id(did, 0) != make_chunk_id(did, 1)


# ---------------------------------------------------------------- 切片

def test_chunk_respects_max_len():
    text = "\n\n".join(["段落"] * 400)   # 远超 MAX_CHUNK
    chunks = chunk_text(text, document_id="kdoc_x")
    assert chunks, "应有切片"
    assert all(len(c.text) <= MAX_CHUNK for c in chunks)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_chunk_splits_overlong_paragraph():
    """单个超长段（无空行）也要被硬切，不能产生超上限的块。"""
    text = "y" * (MAX_CHUNK * 2 + 50)
    chunks = chunk_text(text, document_id="kdoc_x")
    assert len(chunks) >= 3
    assert all(len(c.text) <= MAX_CHUNK for c in chunks)


def test_chunk_empty_text_yields_nothing():
    assert chunk_text("   \n\n  \n", document_id="kdoc_x") == []


# ---------------------------------------------------------------- 切词与打分

def test_query_terms_dedup_and_lowercase():
    terms = query_terms("Motor motor TEMP")
    assert terms.count("motor") == 1          # 去重
    assert "temp" in terms and "TEMP" not in terms   # 小写化


def test_query_terms_cjk_char_split():
    """CJK 无空格：逐字成词（不依赖分词库）。"""
    terms = query_terms("电机温度")
    assert terms == ["电", "机", "温", "度"]


def test_query_terms_drops_single_ascii():
    assert "a" not in query_terms("a b motor")   # 单字符 ASCII 丢弃


def test_score_uses_distinct_terms():
    """打分 = 命中不同词 / 查询词总数，一个高频词不能刷分。"""
    terms = query_terms("motor temp")
    # 只反复出现 motor，缺 temp → 只算命中 1/2
    assert score_chunk("motor motor motor motor", terms) == 0.5
    assert score_chunk("motor temp", terms) == 1.0
    assert score_chunk("nothing here", terms) == 0.0


def test_rank_chunks_orders_by_score_and_attaches_field():
    chunks = [
        {"chunk_id": "a", "text": "battery soc nominal"},
        {"chunk_id": "b", "text": "motor temperature overheat"},
        {"chunk_id": "c", "text": "motor speed"},
    ]
    ranked = rank_chunks(chunks, "motor temperature")
    assert ranked[0]["chunk_id"] == "b"       # 命中两个词
    assert ranked[0]["score"] == 1.0
    assert all("score" in r for r in ranked)
    # 完全不命中的被过滤（min_score 默认 0）
    assert "a" not in [r["chunk_id"] for r in ranked]


def test_rank_empty_query_returns_nothing():
    assert rank_chunks([{"text": "anything"}], "") == []
