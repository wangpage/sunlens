"""把会话动作/转写、技能手册向量化入库。"""

from __future__ import annotations

import numpy as np

from engine.config import SunLensConfig
from engine.memory.embed import embed_texts
from engine.store.db import DB


def _norm_bytes(v) -> bytes:
    a = np.asarray(v, dtype="float32")
    n = float(np.linalg.norm(a))
    if n:
        a = a / n
    return a.tobytes()


def index_session(config: SunLensConfig, db: DB, session_id: str) -> int:
    """索引一个会话的动作描述(含旁白)+转写。重复索引会先清后建。"""
    items: list[tuple[str, int, str]] = []
    for a in db.list_action_steps(session_id):
        txt = (a["nl_description"] or "")
        if a["narration"]:
            txt += " " + a["narration"]
        if txt.strip():
            items.append(("action", a["id"], txt.strip()))
    for t in db.list_transcripts(session_id):
        if t["text"].strip():
            items.append(("transcript", t["id"], t["text"].strip()))
    if not items:
        return 0
    vecs = embed_texts(config, [t for _, _, t in items])
    db.delete_vectors(session_id=session_id)  # 干净重建
    rows = [{"kind": k, "ref_id": r, "session_id": session_id, "text": t,
             "dim": len(v), "vec": _norm_bytes(v)}
            for (k, r, t), v in zip(items, vecs)]
    db.insert_vectors(rows)
    return len(rows)


def index_manual(config: SunLensConfig, db: DB, manual_id: int) -> int:
    """索引一篇技能手册（标题+正文，截断到 2000 字）。"""
    m = db.get_manual(manual_id)
    if not m:
        return 0
    text = (m["title"] + "\n" + (m["content"] or ""))[:2000]
    vec = embed_texts(config, [text])[0]
    db.delete_vectors(kind="manual", ref_id=manual_id)
    db.insert_vectors([{"kind": "manual", "ref_id": manual_id,
                        "session_id": m["session_id"], "text": m["title"],
                        "dim": len(vec), "vec": _norm_bytes(vec)}])
    return 1
