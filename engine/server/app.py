"""本地仪表盘 HTTP 服务（stdlib，零额外依赖）。

提供三栏仪表盘 + JSON API + 帧图。前端轮询刷新实现"边录边看"。
仅监听 127.0.0.1。数据来自 SQLite。

路由：
  GET /                          仪表盘 HTML
  GET /api/sessions              会话列表(带计数)
  GET /api/session/<id>          某会话概览 + 时间轴(帧/动作合并按时间)
  GET /api/frame/<frame_id>      帧 JPEG
  GET /api/search?q=             跨会话关键词检索
  POST /api/ask                  对历史本地关键词检索问答
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from loguru import logger

from engine.config import SunLensConfig
from engine.store.db import DB

_STATIC = Path(__file__).parent / "static"


def _merge_timeline(db: DB, session_id: str) -> list[dict]:
    """把动作合并成一条按时间排序的时间轴；没理解时退化为按帧展示。"""
    items: list[dict] = []
    for a in db.list_action_steps(session_id):
        items.append({"ts": a["ts"], "kind": "action", "type": a["type"],
                      "app": a["target_app"], "text": a["nl_description"],
                      "target": a["target_text"], "frame_id": a["frame_id"]})
    if not items:
        for f in db.list_frames(session_id, limit=2000):
            items.append({"ts": f["ts"], "kind": "frame",
                          "frame_id": f["id"], "trigger": f["trigger"]})
    items.sort(key=lambda i: i["ts"])
    return items


class _Handler(BaseHTTPRequestHandler):
    config: SunLensConfig
    db: DB
    lock: threading.Lock

    def log_message(self, *_: object) -> None:  # 静音默认访问日志
        pass

    # ---- 输出辅助 ----
    def _json(self, obj: object, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _q(self):
        with self.lock:
            return self.db  # 同一连接 + 锁串行化

    # ---- GET ----
    def do_GET(self) -> None:
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        try:
            if path == "/":
                self._bytes((_STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/sessions":
                with self.lock:
                    out = []
                    for s in self.db.list_sessions(limit=100):
                        s["counts"] = self.db.counts(s["id"])
                        out.append(s)
                self._json(out)
            elif path.startswith("/api/session/"):
                sid = path[len("/api/session/"):]
                with self.lock:
                    sess = self.db.get_session(sid)
                    if not sess:
                        return self._json({"error": "not found"}, 404)
                    sess["counts"] = self.db.counts(sid)
                    timeline = _merge_timeline(self.db, sid)
                self._json({"session": sess, "timeline": timeline})
            elif path.startswith("/api/frame/"):
                fid = path[len("/api/frame/"):]
                self._serve_frame(fid)
            elif path == "/api/search":
                q = (qs.get("q", [""])[0]).strip()
                with self.lock:
                    res = self.db.search(q) if q else []
                self._json(res)
            elif path == "/api/manuals":
                with self.lock:
                    self._json(self.db.list_manuals())
            elif path.startswith("/api/manual/"):
                mid = int(path[len("/api/manual/"):])
                with self.lock:
                    m = self.db.get_manual(mid)
                self._json(m or {"error": "not found"}, 200 if m else 404)
            elif path.startswith("/api/intent/"):
                sid = path[len("/api/intent/"):]
                with self.lock:
                    row = self.db.get_intent(sid)
                self._json(json.loads(row["data"]) if row else {})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # pragma: no cover
            logger.warning("请求处理出错 {}: {}", path, e)
            self._json({"error": str(e)}, 500)

    def _serve_frame(self, fid: str) -> None:
        with self.lock:
            row = self.db.conn.execute(
                "SELECT image_path FROM frame WHERE id = ?", (fid,)
            ).fetchone()
        if not row:
            return self._json({"error": "no frame"}, 404)
        p = Path(row["image_path"])
        if not p.exists():
            return self._json({"error": "file missing"}, 404)
        self._bytes(p.read_bytes(), "image/jpeg")

    # ---- POST ----
    def do_POST(self) -> None:
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if u.path == "/api/ask":
            self._ask(body)
        elif u.path == "/api/studio/sop":
            self._sop(body)
        elif u.path == "/api/intent":
            self._intent(body)
        elif u.path == "/api/intent/correct":
            self._correct(body)
        else:
            self._json({"error": "not found"}, 404)

    def _intent(self, body: dict) -> None:
        """会话级意图分析（草稿→反思），存库并返回。"""
        sid = (body.get("session_id") or "").strip()
        if not sid:
            return self._json({"error": "请先在左侧选中一个会话。"}, 200)
        db = None
        try:
            from engine.intent import analyze_intent
            db = self._bg_db()  # 独立连接，不持 self.lock，期间仪表盘照常响应
            self._json(analyze_intent(self.config, db, sid))
        except Exception as e:
            logger.warning("意图分析出错: {}", e)
            self._json({"error": str(e)}, 200)
        finally:
            if db:
                db.close()

    def _correct(self, body: dict) -> None:
        """记录用户对意图理解的纠正（反馈记忆）。"""
        sid = (body.get("session_id") or "").strip() or None
        text = (body.get("text") or "").strip()
        if not text:
            return self._json({"error": "纠正内容为空。"}, 200)
        with self.lock:
            self.db.add_feedback(sid, text)
        self._json({"ok": True})

    def _bg_db(self):
        """给长耗时模型任务单开一条数据库连接，避免占用全局锁把仪表盘卡死。

        SQLite 处于 WAL 模式，多连接可并发读 + 串行写；快接口仍走共享连接 self.db。
        """
        from engine.store.db import DB

        return DB(self.config.data_dir / "sunlens.db").connect()

    def _sop(self, body: dict) -> None:
        """把选中会话的动作流总结成 SOP（本地模型，存进技能手册）。"""
        sid = (body.get("session_id") or "").strip()
        if not sid:
            return self._json({"type": "error", "error": "请先在左侧选中一个会话。"}, 200)
        db = None
        try:
            from engine.studio import generate_sop
            db = self._bg_db()  # 独立连接，不持 self.lock，期间仪表盘照常响应
            self._json(generate_sop(self.config, db, sid))
        except Exception as e:
            logger.warning("生成 SOP 出错: {}", e)
            self._json({"type": "error", "error": str(e)}, 200)
        finally:
            if db:
                db.close()

    def _ask(self, body: dict) -> None:
        """接地问答：本地模型基于已记录/已理解的记忆作答（非关键词匹配）。"""
        q = (body.get("q") or "").strip()
        if not q:
            return self._json({"answer": "请输入问题。"})
        db = None
        try:
            from engine.qa import answer_question
            db = self._bg_db()  # 独立连接，不持 self.lock；模型调用期间仪表盘照常
            self._json(answer_question(self.config, db, q))
        except Exception as e:
            logger.warning("问答出错: {}", e)
            self._json({"answer": f"出错了：{e}"}, 200)
        finally:
            if db:
                db.close()


def serve(config: SunLensConfig, port: int = 8088) -> None:
    db = DB(config.data_dir / "sunlens.db").connect()
    _Handler.config = config
    _Handler.db = db
    _Handler.lock = threading.Lock()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    logger.info("仪表盘已启动：http://127.0.0.1:{}  (Ctrl+C 停止)", port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("仪表盘停止。")
    finally:
        httpd.server_close()
        db.close()
