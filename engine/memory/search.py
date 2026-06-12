"""语义检索：query 向量化 → 与库内向量余弦 → top-k。"""

from __future__ import annotations

import numpy as np

from engine.config import SunLensConfig
from engine.memory.embed import embed_texts
from engine.store.db import DB


def _matrix(rows: list[dict]) -> np.ndarray:
    return np.stack([np.frombuffer(r["vec"], dtype="float32") for r in rows])


def semantic_search(config: SunLensConfig, db: DB, query: str, k: int | None = None) -> list[dict]:
    """语义检索，返回带相似度分的命中项（已归一化向量 → 点积即余弦）。"""
    rows = db.all_vectors()
    if not rows or not query.strip():
        return []
    k = k or config.rag_top_k
    qv = np.asarray(embed_texts(config, [query])[0], dtype="float32")
    n = float(np.linalg.norm(qv))
    if n:
        qv = qv / n
    sims = _matrix(rows) @ qv
    order = np.argsort(-sims)[:k]
    return [{"kind": rows[i]["kind"], "ref_id": rows[i]["ref_id"],
             "session_id": rows[i]["session_id"], "text": rows[i]["text"],
             "score": round(float(sims[i]), 4)} for i in order]


def retrieve(config: SunLensConfig, db: DB, query: str, k: int | None = None) -> list[str]:
    """理解增强用：返回相似历史的文本片段（best-effort，出错返回空）。"""
    try:
        hits = semantic_search(config, db, query, k or config.rag_augment_k)
        return [h["text"] for h in hits if h["text"]]
    except Exception:
        return []
