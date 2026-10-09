"""`knowledge` 域端点：从 create_app 内联定义按域拆出。

函数体一字未改，只把 `@router.x` 换成 `@router.x`；装配在 service/app.py。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ...knowledge_ingest import chunk_text, make_document_id, rank_chunks, sha256_text
from ...storage import repository as repo
from ...storage.db import session_scope
from ..deps import require_tenant

router = APIRouter()


@router.post("/v1/knowledge/documents", tags=["knowledge"])
def import_knowledge_document(
    payload: dict[str, Any],
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """导入一份**用户自带**厂商文档（纯文本 → 切片 → 落库，租户隔离）。

    正文：`{"source_name": "...", "text": "..."}`。`text` 是**已抽取的纯文本**；
    PDF/DOCX 解析在上游（本项目不做二进制解析）。

    **诚实标注**：本通道的检索是**词法检索**，与内置 chroma 语料（向量语义
    检索）互补、非替代。用户文档按租户隔离——别人的厂商原件你检索不到。
    """
    body = payload or {}
    text = body.get("text")
    source_name = str(body.get("source_name") or "untitled")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=422, detail="正文需含非空 'text' 字符串")
    if len(text) > 2_000_000:
        raise HTTPException(status_code=422, detail="单份文档上限 200 万字符")

    digest = sha256_text(text)
    document_id = make_document_id(digest)
    chunks = chunk_text(text, document_id=document_id)
    with session_scope() as s:
        repo.save_knowledge_document(
            s, tenant_id=tenant,
            meta={"document_id": document_id, "source_name": source_name,
                  "content_sha256": digest, "char_count": len(text)},
            chunks=[{"chunk_id": c.chunk_id, "ordinal": c.ordinal,
                     "text": c.text} for c in chunks])
    return {"ok": True, "document_id": document_id, "source_name": source_name,
            "chunk_count": len(chunks), "char_count": len(text)}
@router.get("/v1/knowledge/documents", tags=["knowledge"])
def list_knowledge_documents(
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """列出本租户已导入的厂商文档。"""
    with session_scope() as s:
        items = repo.list_knowledge_documents(s, tenant_id=tenant)
    return {"ok": True, "count": len(items), "items": items}
@router.get("/v1/knowledge/search", tags=["knowledge"])
def search_knowledge(
    q: str = Query(..., min_length=1),
    tenant: str = Depends(require_tenant),
    limit: int = Query(default=5, ge=1, le=50),
    document_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """在本租户**已导入**的厂商文档里做**词法检索**，返回命中的切片。

    这是"用户自带文档"的检索通道：确定性、可离线、零依赖。**不是**向量语义
    检索——同义词/近义表述召回请用内置 chroma 语料通道。两者互补。
    """
    with session_scope() as s:
        chunks = repo.list_knowledge_chunks(s, tenant_id=tenant,
                                            document_id=document_id)
        docs = {d["document_id"]: d["source_name"]
                for d in repo.list_knowledge_documents(s, tenant_id=tenant)}
    hits = rank_chunks(chunks, q)[:limit]
    for h in hits:
        h["source_name"] = docs.get(h["document_id"], "")
    return {"ok": True, "query": q, "count": len(hits), "hits": hits}
@router.delete("/v1/knowledge/documents/{document_id}", tags=["knowledge"])
def delete_knowledge_document(
    document_id: str,
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """删一份用户导入的厂商文档及其切片（租户内；不存在 → 404）。"""
    with session_scope() as s:
        ok = repo.delete_knowledge_document(s, document_id, tenant_id=tenant)
    if not ok:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"ok": True, "document_id": document_id, "deleted": True}
