"""把「帧 + 输入事件 + 语音转写」融合成结构化 ActionStep（M3）。

对一个已录会话的每个关键帧：
  1. 收集该帧时间窗内你的输入事件（点击/打字）和对方同时说的话；
  2. 本机脱敏涂码（发云端前必须，复用 §5.2/§5.3）；
  3. 过出口闸门 → 交给理解后端(默认 Qwen-VL 云，可注入 mock)；
  4. 合成 ActionStep 落库。

后端依赖注入：测试时传 mock，无需 API Key。
"""

from __future__ import annotations

from loguru import logger
from PIL import Image

from engine.config import SunLensConfig
from engine.privacy.gate import check_egress_allowed
from engine.privacy.scrubber import scrub_image
from engine.store.db import DB
from engine.understand.backend import FrameContext, UnderstandBackend


def _summarize_inputs(events: list[dict]) -> list[str]:
    """把一窗输入事件压成可读短语，连续字符按键合并成'输入xxx'。"""
    out: list[str] = []
    typed: list[str] = []

    def flush_typed() -> None:
        if typed:
            out.append("输入'" + "".join(typed) + "'")
            typed.clear()

    for e in events:
        kind = e["kind"]
        if kind == "click":
            flush_typed()
            out.append(f"点击({e['x']},{e['y']})")
        elif kind == "scroll":
            flush_typed()
            out.append("滚动")
        elif kind == "key_press":
            k = e.get("key")
            if k and len(k) == 1:
                typed.append(k)
            else:
                flush_typed()
                out.append(f"按{k}")
    flush_typed()
    return out


def _overlaps(t: dict, lo: str, hi: str) -> bool:
    """转写片段 [ts_start,ts_end] 是否与时间窗 [lo,hi] 相交（ISO 串比较）。"""
    return t["ts_start"] <= hi and t["ts_end"] >= lo


class ActionBuilder:
    """会话级理解：帧流 → ActionStep 流。"""

    def __init__(self, config: SunLensConfig, db: DB,
                 backend: UnderstandBackend | None = None) -> None:
        self.config = config
        self.db = db
        self._backend = backend

    def _get_backend(self) -> UnderstandBackend:
        if self._backend is None:
            from engine.understand.qwen_cloud import QwenCloudBackend

            self._backend = QwenCloudBackend(self.config)
        return self._backend

    def build_session(self, session_id: str, *, scrub: bool = True) -> int:
        session = self.db.get_session(session_id)
        if session is None:
            raise ValueError(f"未知会话: {session_id}")
        title = session.get("host_window_title", "")

        frames = self.db.list_frames(session_id, limit=10000)
        inputs = self.db.list_input_events(session_id)
        transcripts = self.db.list_transcripts(session_id)
        if not frames:
            logger.warning("会话 {} 无帧，跳过。", session_id)
            return 0

        backend = self._get_backend()
        prev_ts = session.get("started_at", frames[0]["ts"])
        count = 0

        for f in frames:
            lo, hi = prev_ts, f["ts"]
            win_inputs = [e for e in inputs if lo <= e["ts"] <= hi]
            win_narr = [t for t in transcripts if _overlaps(t, lo, hi)]
            narration = " ".join(f"{t['speaker']}:{t['text']}" for t in win_narr)
            last_click = next((e for e in reversed(win_inputs) if e["kind"] == "click"), None)

            try:
                img = Image.open(f["image_path"])
            except Exception as e:
                logger.warning("打开帧失败 {}: {}", f["image_path"], e)
                prev_ts = f["ts"]
                continue

            if scrub:
                img, _reds = scrub_image(img, self.config)
            check_egress_allowed(
                scrubbed=scrub, redact_enabled=self.config.redact_enabled,
                allow_unredacted=not scrub,
            )

            recent = _summarize_inputs(win_inputs)
            # demo-conditioned：检索相似历史增强理解（跨会话；本会话尚未索引）
            history: list[str] = []
            if self.config.memory_enabled and self.config.dashscope_api_key:
                from engine.memory.search import retrieve
                query = " ".join([title, *recent] + ([narration] if narration else []))
                history = retrieve(self.config, self.db, query)

            ctx = FrameContext(
                app_window_title=title,
                click_xy=(last_click["x"], last_click["y"]) if last_click else None,
                timestamp=f["ts"],
                recent_inputs=recent,
                narration=narration,
                history=history,
            )
            u = backend.describe_frame(img, ctx)

            self.db.insert_action_step(
                session_id, f["id"], f["ts"],
                type=u.action_type or None,
                target_app=u.app or None,
                target_text=u.target or u.search_query or None,
                bbox=None,
                narration=narration or None,
                nl_description=u.description or None,
                confidence=None,
            )
            count += 1
            prev_ts = f["ts"]

        # 理解完即索引进语义记忆，供以后检索/增强
        if self.config.memory_enabled and self.config.dashscope_api_key:
            try:
                from engine.memory.index import index_session
                n = index_session(self.config, self.db, session_id)
                logger.info("已索引 {} 条进语义记忆。", n)
            except Exception as e:  # pragma: no cover
                logger.warning("索引语义记忆失败: {}", e)

        logger.info("会话 {} 理解完成，生成 {} 个 ActionStep。", session_id, count)
        return count
