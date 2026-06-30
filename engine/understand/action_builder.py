"""把「帧 + 输入事件」融合成结构化 ActionStep。

对一个已录会话的每个关键帧：
  1. 收集该帧时间窗内你的输入事件（点击/打字）；
  2. 交给本地 qwen3-vl 看懂（vlm.describe_frame）；
  3. 合成 ActionStep 落库。

本地推理，画面不出本机，无需脱敏/出口闸门。
"""

from __future__ import annotations

from loguru import logger
from PIL import Image

from engine.capture.framediff import _BITS, hamming
from engine.config import SunLensConfig
from engine.store.db import DB
from engine.understand.vlm import FrameContext, describe_frame


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


class ActionBuilder:
    """会话级理解：帧流 → ActionStep 流。"""

    def __init__(self, config: SunLensConfig, db: DB) -> None:
        self.config = config
        self.db = db

    def _select_keyframes(self, frames: list[dict]) -> list[dict]:
        """只保留视觉显著变化的关键帧，丢掉"同屏幕重复"的冗余帧。

        逐帧比 aHash 与「上一个被保留的关键帧」的汉明距离，超阈值才留；
        其间的输入事件由 build_session 归并进下一个关键帧的上下文，不丢操作。
        关键帧过多时再按上限均匀抽样。
        """
        th = self.config.keyframe_diff_threshold
        kept: list[dict] = []
        last: int | None = None
        for f in frames:
            ah = f.get("ahash")
            h = int(ah, 16) if ah else None
            if last is None or h is None or hamming(h, last) / _BITS >= th:
                kept.append(f)
                if h is not None:
                    last = h
        cap = self.config.understand_max_frames
        if cap and len(kept) > cap:
            step = len(kept) / cap
            kept = [kept[int(i * step)] for i in range(cap)]
        return kept

    def build_session(self, session_id: str) -> int:
        session = self.db.get_session(session_id)
        if session is None:
            raise ValueError(f"未知会话: {session_id}")
        title = session.get("host_window_title", "")

        all_frames = self.db.list_frames(session_id, limit=10000)
        inputs = self.db.list_input_events(session_id)
        if not all_frames:
            logger.warning("会话 {} 无帧，跳过。", session_id)
            return 0

        frames = self._select_keyframes(all_frames)
        logger.info("关键帧采样：{} 帧 → {} 关键帧（省 {} 次模型调用）。",
                    len(all_frames), len(frames), len(all_frames) - len(frames))

        prev_ts = session.get("started_at", all_frames[0]["ts"])
        count = 0
        history: list[str] = []  # 已理解步骤的滚动叙事上下文

        for f in frames:
            lo, hi = prev_ts, f["ts"]
            win_inputs = [e for e in inputs if lo <= e["ts"] <= hi]
            last_click = next((e for e in reversed(win_inputs) if e["kind"] == "click"), None)

            try:
                img = Image.open(f["image_path"])
            except Exception as e:
                logger.warning("打开帧失败 {}: {}", f["image_path"], e)
                prev_ts = f["ts"]
                continue

            ctx = FrameContext(
                app_window_title=title,
                click_xy=(last_click["x"], last_click["y"]) if last_click else None,
                timestamp=f["ts"],
                recent_inputs=_summarize_inputs(win_inputs),
                history=history[-5:],  # 最近 5 步作为叙事上下文，不再孤立描述
            )
            u = describe_frame(self.config, img, ctx)
            if u.description:
                history.append(f"{(f['ts'] or '')[11:19]} {u.description}")

            self.db.insert_action_step(
                session_id, f["id"], f["ts"],
                type=u.action_type or None,
                target_app=u.app or None,
                target_text=u.target or u.search_query or None,
                bbox=None,
                narration=None,
                nl_description=u.description or None,
                confidence=None,
            )
            count += 1
            prev_ts = f["ts"]

        logger.info("会话 {} 理解完成，生成 {} 个 ActionStep。", session_id, count)
        return count
