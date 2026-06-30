"""SQLite 索引（借 openadapt-desktop/engine/db.py 的 WAL + Row + 参数化模式）。

M1 只建三张表：session / frame / input_event。后续里程碑再加
transcript / action_step / skill_manual 等（见 ARCHITECTURE §6）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS session (
    id              TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    status          TEXT NOT NULL DEFAULT 'recording',  -- recording/completed/recovered
    remote_label    TEXT DEFAULT '',
    host_window_title TEXT DEFAULT '',
    dir             TEXT NOT NULL,
    pid             INTEGER,
    frame_count     INTEGER DEFAULT 0,
    input_count     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS frame (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES session(id),
    ts          TEXT NOT NULL,
    image_path  TEXT NOT NULL,
    ahash       TEXT,
    diff_score  REAL,
    trigger     TEXT  -- visual_change/input/idle/first
);

CREATE TABLE IF NOT EXISTS input_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES session(id),
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,   -- click/scroll/key_press/key_release
    x           INTEGER,
    y           INTEGER,
    button      TEXT,
    key         TEXT,
    pressed     INTEGER
);

CREATE TABLE IF NOT EXISTS transcript (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES session(id),
    ts_start    TEXT NOT NULL,    -- 绝对墙钟时间
    ts_end      TEXT NOT NULL,
    speaker     TEXT NOT NULL,    -- 你 / 对方
    text        TEXT NOT NULL,
    confidence  REAL
);

CREATE TABLE IF NOT EXISTS action_step (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES session(id),
    frame_id        INTEGER REFERENCES frame(id),
    ts              TEXT NOT NULL,
    type            TEXT,            -- click/input/search/open/switch/other
    target_app      TEXT,
    target_text     TEXT,            -- 搜索词/目标元素文本
    bbox            TEXT,            -- 元素 bbox(M4 grounding 后填)
    narration       TEXT,            -- 同时段对方说的话
    nl_description  TEXT,            -- 自然语言描述
    confidence      REAL
);

CREATE TABLE IF NOT EXISTS skill_manual (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT REFERENCES session(id),
    title       TEXT NOT NULL,
    kind        TEXT NOT NULL,    -- sop/report/...（产出类型）
    content     TEXT NOT NULL,    -- markdown
    path        TEXT,             -- skills/ 下的文件
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vector (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,    -- action/transcript/manual
    ref_id      INTEGER,
    session_id  TEXT,
    text        TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vec         BLOB NOT NULL     -- 归一化 float32
);

CREATE TABLE IF NOT EXISTS session_intent (
    session_id  TEXT PRIMARY KEY REFERENCES session(id),
    data        TEXT NOT NULL,    -- JSON：task/why/info_sought/outcome/confidence/reflection
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intent_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT,
    correction  TEXT NOT NULL,    -- 用户对意图理解的纠正（喂回模型当上下文，越用越准）
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vector_session ON vector(session_id);
CREATE INDEX IF NOT EXISTS idx_vector_kind ON vector(kind);
CREATE INDEX IF NOT EXISTS idx_frame_session ON frame(session_id);
CREATE INDEX IF NOT EXISTS idx_input_session ON input_event(session_id);
CREATE INDEX IF NOT EXISTS idx_transcript_session ON transcript(session_id);
CREATE INDEX IF NOT EXISTS idx_action_session ON action_step(session_id);
CREATE INDEX IF NOT EXISTS idx_session_status ON session(status);
"""

_SESSION_COLUMNS = {
    "started_at", "ended_at", "status", "remote_label",
    "host_window_title", "dir", "pid", "frame_count", "input_count",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DB:
    """SunLens 的 SQLite 句柄 + DAO。"""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._conn: sqlite3.Connection | None = None

    # ---- 生命周期 ----
    def connect(self) -> "DB":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        return self

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("DB 未连接，请先 connect()。")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- session ----
    def insert_session(self, session_id: str, started_at: str, dir_: str, *, pid: int,
                       host_window_title: str = "", remote_label: str = "") -> None:
        self.conn.execute(
            "INSERT INTO session (id, started_at, status, dir, pid, host_window_title, remote_label)"
            " VALUES (?, ?, 'recording', ?, ?, ?, ?)",
            (session_id, started_at, dir_, pid, host_window_title, remote_label),
        )
        self.conn.commit()

    def update_session(self, session_id: str, **fields: object) -> None:
        if not fields:
            return
        bad = set(fields) - _SESSION_COLUMNS
        if bad:
            raise ValueError(f"未知的 session 字段: {bad}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE session SET {sets} WHERE id = ?", [*fields.values(), session_id]
        )
        self.conn.commit()

    def get_session(self, session_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM session WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def list_sessions(self, limit: int = 20, status: str | None = None) -> list[dict]:
        q = "SELECT * FROM session"
        params: list = []
        if status:
            q += " WHERE status = ?"
            params.append(status)
        q += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]

    # ---- frame ----
    def insert_frame(self, session_id: str, ts: str, image_path: str,
                     ahash: str | None, diff_score: float | None, trigger: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO frame (session_id, ts, image_path, ahash, diff_score, trigger)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, ts, image_path, ahash, diff_score, trigger),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_frames(self, session_id: str, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM frame WHERE session_id = ? ORDER BY ts LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- transcript ----
    def insert_transcript(self, session_id: str, ts_start: str, ts_end: str,
                          speaker: str, text: str, confidence: float | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO transcript (session_id, ts_start, ts_end, speaker, text, confidence)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, ts_start, ts_end, speaker, text, confidence),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_transcripts(self, session_id: str, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM transcript WHERE session_id = ? ORDER BY ts_start LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- input_event ----
    def insert_input_events(self, session_id: str, events: list[dict]) -> int:
        if not events:
            return 0
        self.conn.executemany(
            "INSERT INTO input_event (session_id, ts, kind, x, y, button, key, pressed)"
            " VALUES (:session_id, :ts, :kind, :x, :y, :button, :key, :pressed)",
            [{"session_id": session_id, **e} for e in events],
        )
        self.conn.commit()
        return len(events)

    def list_input_events(self, session_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM input_event WHERE session_id = ? ORDER BY ts", (session_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- action_step ----
    def insert_action_step(self, session_id: str, frame_id: int | None, ts: str, *,
                           type: str | None, target_app: str | None, target_text: str | None,
                           bbox: str | None, narration: str | None,
                           nl_description: str | None, confidence: float | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO action_step (session_id, frame_id, ts, type, target_app, target_text,"
            " bbox, narration, nl_description, confidence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, frame_id, ts, type, target_app, target_text, bbox,
             narration, nl_description, confidence),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_action_steps(self, session_id: str, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM action_step WHERE session_id = ? ORDER BY ts LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- 聚合 / 检索（M4 仪表盘）----
    def counts(self, session_id: str) -> dict:
        def c(table: str) -> int:
            return self.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
        return {"frames": c("frame"), "inputs": c("input_event"),
                "transcripts": c("transcript"), "actions": c("action_step")}

    # ---- skill_manual（M5）----
    def insert_manual(self, session_id: str | None, title: str, kind: str,
                      content: str, path: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO skill_manual (session_id, title, kind, content, path, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, title, kind, content, path, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def delete_session_manuals(self, session_id: str, kind: str | None = None) -> None:
        """删除某会话的手册（同会话重新生成时先删后插，避免重复堆积）。"""
        if kind:
            self.conn.execute(
                "DELETE FROM skill_manual WHERE session_id = ? AND kind = ?", (session_id, kind)
            )
        else:
            self.conn.execute("DELETE FROM skill_manual WHERE session_id = ?", (session_id,))
        self.conn.commit()

    def list_manuals(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, session_id, title, kind, path, created_at FROM skill_manual"
            " ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_manual(self, manual_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM skill_manual WHERE id = ?", (manual_id,)
        ).fetchone()
        return dict(row) if row else None

    # ---- vector（语义记忆 / RAG）----
    def insert_vectors(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        self.conn.executemany(
            "INSERT INTO vector (kind, ref_id, session_id, text, dim, vec)"
            " VALUES (:kind, :ref_id, :session_id, :text, :dim, :vec)", rows,
        )
        self.conn.commit()
        return len(rows)

    def all_vectors(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, kind, ref_id, session_id, text, dim, vec FROM vector"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_vectors(self, *, kind: str | None = None, session_id: str | None = None,
                       ref_id: int | None = None) -> None:
        conds, params = [], []
        if kind is not None:
            conds.append("kind = ?"); params.append(kind)
        if session_id is not None:
            conds.append("session_id = ?"); params.append(session_id)
        if ref_id is not None:
            conds.append("ref_id = ?"); params.append(ref_id)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        self.conn.execute(f"DELETE FROM vector{where}", params)
        self.conn.commit()

    def count_vectors(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM vector").fetchone()[0]

    def search(self, query: str, limit: int = 80) -> list[dict]:
        """跨会话关键词检索动作描述 + 转写（M6 升级为语义/LLM）。"""
        like = f"%{query}%"
        rows = self.conn.execute(
            "SELECT session_id, ts AS ts, nl_description AS text, 'action' AS kind"
            " FROM action_step WHERE nl_description LIKE ?"
            " UNION ALL "
            "SELECT session_id, ts_start AS ts, text, 'transcript' AS kind"
            " FROM transcript WHERE text LIKE ?"
            " ORDER BY ts LIMIT ?",
            (like, like, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- 意图理解（会话级 task/why/info_sought/outcome + 反思）----
    def upsert_intent(self, session_id: str, data_json: str) -> None:
        self.conn.execute(
            "INSERT INTO session_intent (session_id, data, created_at) VALUES (?, ?, ?)"
            " ON CONFLICT(session_id) DO UPDATE SET data=excluded.data, created_at=excluded.created_at",
            (session_id, data_json, _now()),
        )
        self.conn.commit()

    def get_intent(self, session_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT data, created_at FROM session_intent WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    # ---- 反馈记忆（用户纠正 → 下次作为上下文喂回，越用越准）----
    def add_feedback(self, session_id: str | None, correction: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO intent_feedback (session_id, correction, created_at) VALUES (?, ?, ?)",
            (session_id, correction, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_feedback(self, limit: int = 5) -> list[dict]:
        rows = self.conn.execute(
            "SELECT session_id, correction, created_at FROM intent_feedback"
            " ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
