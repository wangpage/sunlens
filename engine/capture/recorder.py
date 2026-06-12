"""录制控制器（借 openadapt-desktop/controller.py 的生命周期 + state.json 恢复）。

事件驱动抓帧主循环：低频轮询远控窗口 → aHash 去重 → 仅画面变化/有输入/idle 时落帧；
你的鼠标键盘事件全程累积落库。窗口消失超过 grace 即自动结束会话。
"""

from __future__ import annotations

import json
import os
import signal
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from engine.capture.detector import pick_remote_window
from engine.capture.framediff import average_hash, diff_ratio, hash_to_hex
from engine.capture.input_hook import InputHook
from engine.capture.window_grabber import capture_window
from engine.config import SunLensConfig
from engine.store.db import DB


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class Recorder:
    """一次录制会话的生命周期 + 抓帧循环。"""

    def __init__(self, config: SunLensConfig, db: DB) -> None:
        self.config = config
        self.db = db
        self._stop = False
        self._session_id: str | None = None
        self._session_dir: Path | None = None
        self._hook: InputHook | None = None
        self._audio = None  # AudioSession | None

    # ---- 信号 ----
    def request_stop(self, *_: object) -> None:
        logger.info("收到停止信号，正在收尾……")
        self._stop = True

    def _install_signals(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.request_stop)
            except ValueError:
                pass  # 非主线程

    # ---- 主流程 ----
    def start(self) -> str | None:
        """前台阻塞录制，直到收到停止信号或远控窗口消失。返回 session_id。"""
        win = pick_remote_window(self.config)
        if win is None:
            logger.error("未找到向日葵远控窗口，无法开录。先连入远程会话。")
            return None

        session_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
        session_dir = self.config.data_dir / "sessions" / session_id
        (session_dir / "frames").mkdir(parents=True, exist_ok=True)
        self._session_id, self._session_dir = session_id, session_dir

        self.db.insert_session(
            session_id, _now_iso(), str(session_dir),
            pid=os.getpid(), host_window_title=win.title, remote_label=win.title,
        )
        self._write_state("recording", win.title)
        self._write_pidfile()
        self._install_signals()

        # 输入捕获（best-effort）
        if self.config.capture_input_events:
            self._hook = InputHook()
            if self._hook.start():
                logger.info("输入事件捕获已启动。")
            else:
                self._hook = None

        # 音频 + 转写（best-effort，与抓帧并行）
        if self.config.audio_enabled:
            from engine.capture.audio_session import AudioSession

            audio = AudioSession(self.config, session_id, session_dir / "audio")
            self._audio = audio if audio.start() else None

        logger.info("▶ 开始录制 session={} 窗口={!r} ({}x{})", session_id, win.title, win.width, win.height)
        frame_count = input_count = 0
        prev_hash: int | None = None
        last_capture = last_save = 0.0
        window_lost_since: float | None = None

        try:
            while not self._stop:
                t = time.monotonic()
                win = pick_remote_window(self.config)

                if win is None:  # 窗口没了
                    input_count += self._flush_inputs(session_id)
                    if window_lost_since is None:
                        window_lost_since = t
                    elif t - window_lost_since >= self.config.window_lost_grace_secs:
                        logger.info("远控窗口已消失，自动结束会话。")
                        break
                    time.sleep(self.config.poll_interval_secs)
                    continue
                window_lost_since = None

                try:
                    img = capture_window(win.window_id)
                except Exception as e:
                    logger.warning("抓帧失败: {}", e)
                    time.sleep(self.config.poll_interval_secs)
                    continue

                h = average_hash(img)
                diff = diff_ratio(prev_hash, h)
                pending = self._hook.drain() if self._hook else []

                visual = diff >= self.config.frame_diff_threshold
                idle = (t - last_save) >= self.config.idle_capture_interval_secs
                first = prev_hash is None
                should = first or visual or bool(pending) or idle

                if should and (t - last_capture) >= self.config.min_capture_interval_secs:
                    trigger = (
                        "first" if first else
                        "visual_change" if visual else
                        "input" if pending else "idle"
                    )
                    frame_count += 1
                    img_path = session_dir / "frames" / f"{frame_count:06d}.jpg"
                    img.convert("RGB").save(img_path, "JPEG", quality=self.config.jpeg_quality)
                    self.db.insert_frame(
                        session_id, _now_iso(), str(img_path),
                        ahash=hash_to_hex(h), diff_score=round(diff, 4), trigger=trigger,
                    )
                    prev_hash = h
                    last_capture = last_save = t

                if pending:
                    input_count += self.db.insert_input_events(session_id, pending)

                time.sleep(self.config.poll_interval_secs)
        finally:
            self._finalize(frame_count, input_count, status="completed")
        return session_id

    # ---- 收尾 ----
    def _flush_inputs(self, session_id: str) -> int:
        if not self._hook:
            return 0
        ev = self._hook.drain()
        return self.db.insert_input_events(session_id, ev) if ev else 0

    def _finalize(self, frame_count: int, input_count: int, status: str) -> None:
        if self._hook:
            self._hook.stop()
        if self._audio:
            self._audio.stop()
        if self._session_id:
            self.db.update_session(
                self._session_id, status=status, ended_at=_now_iso(),
                frame_count=frame_count, input_count=input_count,
            )
            self._write_state(status, "")
        self._remove_pidfile()
        logger.info("■ 录制结束 session={} 帧={} 输入={}", self._session_id, frame_count, input_count)

    # ---- state.json / pidfile ----
    def _write_state(self, status: str, title: str) -> None:
        if not self._session_dir:
            return
        (self._session_dir / "state.json").write_text(json.dumps({
            "session_id": self._session_id, "status": status,
            "pid": os.getpid(), "host_window_title": title, "updated_at": _now_iso(),
        }, ensure_ascii=False, indent=2))

    def _pidfile(self) -> Path:
        return self.config.data_dir / "recorder.pid"

    def _write_pidfile(self) -> None:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self._pidfile().write_text(str(os.getpid()))

    def _remove_pidfile(self) -> None:
        try:
            self._pidfile().unlink()
        except FileNotFoundError:
            pass


def recover_sessions(config: SunLensConfig, db: DB) -> list[str]:
    """把上次崩溃残留的 'recording' 会话（进程已死）标记为 recovered。"""
    recovered: list[str] = []
    for s in db.list_sessions(limit=100, status="recording"):
        pid = s.get("pid")
        if pid and _is_pid_alive(int(pid)):
            continue  # 还活着，正在录
        db.update_session(s["id"], status="recovered", ended_at=_now_iso())
        recovered.append(s["id"])
    return recovered
