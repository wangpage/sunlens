"""本地仪表盘 HTTP 服务（stdlib，零额外依赖）。

提供三栏仪表盘（NotebookLM 风格）+ JSON API + 帧图。前端轮询刷新实现"边录边看"。
仅监听 127.0.0.1。数据来自 SQLite。

路由：
  GET /                          仪表盘 HTML
  GET /api/sessions              会话列表(带计数)
  GET /api/session/<id>          某会话概览 + 时间轴(帧/动作/转写合并按时间)
  GET /api/frame/<frame_id>      帧 JPEG
  GET /api/search?q=             跨会话关键词检索
  GET /api/manuals               技能手册列表(M5，暂空)
  POST /api/ask                  对会话提问(M4: 关键词检索；M6: LLM/MCP)
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
    """把动作/转写合并成一条按时间排序的时间轴。"""
    items: list[dict] = []
    for a in db.list_action_steps(session_id):
        items.append({"ts": a["ts"], "kind": "action", "type": a["type"],
                      "app": a["target_app"], "text": a["nl_description"],
                      "target": a["target_text"], "frame_id": a["frame_id"]})
    for t in db.list_transcripts(session_id):
        items.append({"ts": t["ts_start"], "kind": "transcript",
                      "speaker": t["speaker"], "text": t["text"]})
    # 若还没理解(无 action)，退化为按帧展示时间轴
    if not any(i["kind"] == "action" for i in items):
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
            elif path.startswith("/api/studio_audio/"):
                self._serve_audio(path[len("/api/studio_audio/"):])
            elif path == "/api/studio/list":
                from engine.studio import PLUGINS
                self._json([{"id": k, "name": v["name"], "needs_key": v["needs_key"]}
                            for k, v in PLUGINS.items()])
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

    def _serve_audio(self, sid: str) -> None:
        p = self.config.data_dir / "sessions" / sid / "audio_overview.m4a"
        if not p.exists():
            return self._json({"error": "no audio"}, 404)
        self._bytes(p.read_bytes(), "audio/mp4")

    # ---- POST ----
    def do_POST(self) -> None:
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if u.path == "/api/ask":
            self._ask(body)
        elif u.path.startswith("/api/studio/"):
            self._studio(u.path[len("/api/studio/"):], body)
        else:
            self._json({"error": "not found"}, 404)

    def _studio(self, plugin_id: str, body: dict) -> None:
        # 兼容单会话(session_id)与多勾选(session_ids)
        sids = body.get("session_ids")
        if not sids:
            one = (body.get("session_id") or "").strip()
            sids = [one] if one else []
        sids = [s for s in sids if s]
        mids = [int(m) for m in (body.get("manual_ids") or [])]
        if not sids and not mids:
            return self._json({"type": "error", "error": "请先在左侧勾选至少一个来源（会话或技能手册）。"}, 200)
        from engine.studio import PLUGINS, run_plugin
        spec = PLUGINS.get(plugin_id)
        if not spec:
            return self._json({"error": f"未知插件 {plugin_id}"}, 404)
        if spec["needs_key"] and not self.config.dashscope_api_key:
            return self._json({"type": "error",
                               "error": f"「{spec['name']}」需要 DASHSCOPE_API_KEY。"}, 200)
        try:
            with self.lock:
                result = run_plugin(self.config, self.db, plugin_id, sids, mids)
            self._json(result)
        except Exception as e:
            logger.warning("插件 {} 出错: {}", plugin_id, e)
            self._json({"type": "error", "error": str(e)}, 200)

    def _ask(self, body: dict) -> None:
        q = (body.get("q") or "").strip()
        mode = body.get("mode", "local")
        if not q:
            return self._json({"answer": "请输入问题。", "mode": mode})

        try:
            if mode == "local":
                # 优先语义检索（有 key + 已索引），否则退回关键词 LIKE
                sem = []
                if self.config.memory_enabled and self.config.dashscope_api_key:
                    try:
                        from engine.memory.search import semantic_search
                        with self.lock:
                            sem = semantic_search(self.config, self.db, q)
                    except Exception as e:
                        logger.warning("语义检索出错，退回关键词: {}", e)
                if sem:
                    answer = f"语义检索到 {len(sem)} 条相关记录（按相似度排序）。"
                    hits = [{"text": h["text"], "session_id": h["session_id"],
                             "ts": "", "kind": h["kind"], "score": h["score"]} for h in sem]
                    return self._json({"answer": answer, "mode": mode, "hits": hits})
                with self.lock:
                    hits = self.db.search(q)
                answer = (f"关键词匹配到 {len(hits)} 条（未建语义索引或无 key）。"
                          if hits else "本地历史没找到。试试『全网搜索』或『深度研究』。")
                return self._json({"answer": answer, "mode": mode, "hits": hits[:20]})

            if mode in ("web", "research"):
                if not self.config.dashscope_api_key:
                    return self._json({"answer": "缺 DASHSCOPE_API_KEY，无法联网问答。", "mode": mode})
                from engine.assist.web import web_answer
                answer = web_answer(self.config, q, deep=(mode == "research"))
                return self._json({"answer": answer, "mode": mode})

            if mode == "image":
                if not self.config.dashscope_api_key:
                    return self._json({"answer": "缺 DASHSCOPE_API_KEY，无法生成图片。", "mode": mode})
                from engine.assist.image import generate_image
                urls = generate_image(self.config, q)
                return self._json({"answer": f"生成 {len(urls)} 张图片。", "mode": mode, "images": urls})

            self._json({"answer": f"未知模式: {mode}", "mode": mode})
        except Exception as e:
            logger.warning("ask[{}] 出错: {}", mode, e)
            self._json({"answer": f"出错了：{e}", "mode": mode}, 200)


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
