"""音频会话：把分轨采集 + 转写 + 落库 串起来（与屏幕抓帧并行）。

- 麦克风轨(你) + 可选 loopback 轨(对方) 各自切段；
- 切好的 chunk 进队列，后台 worker 用 Whisper 转写，PII 过滤后写 transcript 表；
- 转写片段时间戳 = chunk 墙钟起点 + Whisper 段内相对偏移（与屏幕动作可对齐）。

独立开自己的 DB 连接，避免和抓帧主线程争用同一 sqlite 连接。
"""

from __future__ import annotations

import queue
import threading
from datetime import timedelta
from pathlib import Path

from loguru import logger

from engine.capture.audio import AudioChunk, AudioTrack, find_device
from engine.config import SunLensConfig
from engine.privacy.scrubber import _PII_PATTERNS, _SENSITIVE_KEYWORDS
from engine.store.db import DB
from engine.capture.transcribe import Transcriber


def _scrub_text(text: str) -> str:
    """转写文本 PII 过滤（与图像涂码同源规则）：命中即替换为 <REDACTED>。"""
    low = text.lower()
    for kw in _SENSITIVE_KEYWORDS:
        if kw in low:
            return "<含敏感词，已隐去>"
    out = text
    for entity, pat in _PII_PATTERNS:
        out = pat.sub(f"<{entity}>", out)
    return out


class AudioSession:
    """录制期间的音频采集 + 转写。"""

    def __init__(self, config: SunLensConfig, session_id: str, audio_dir: Path) -> None:
        self.config = config
        self.session_id = session_id
        self.audio_dir = audio_dir
        self._db = DB(config.data_dir / "sunlens.db")
        self._tx = Transcriber(config.whisper_model, config.whisper_device,
                               config.whisper_compute_type, config.whisper_language)
        self._tracks: list[AudioTrack] = []
        self._q: "queue.Queue[AudioChunk | None]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self.transcript_count = 0

    def _enqueue(self, chunk: AudioChunk) -> None:
        self._q.put(chunk)

    def _build_tracks(self) -> None:
        sr, chunk = self.config.audio_samplerate, self.config.audio_chunk_secs
        # 麦克风 = 你
        self._tracks.append(AudioTrack(
            device=find_device(self.config.mic_device), speaker="你",
            samplerate=sr, chunk_secs=chunk, out_dir=self.audio_dir, on_chunk=self._enqueue,
        ))
        # loopback = 对方（设备存在才加）
        sys_idx = find_device(self.config.system_audio_device)
        if sys_idx is not None:
            self._tracks.append(AudioTrack(
                device=sys_idx, speaker="对方",
                samplerate=sr, chunk_secs=chunk, out_dir=self.audio_dir, on_chunk=self._enqueue,
            ))
        else:
            logger.info("未找到 loopback 设备 {!r}，只录麦克风（你）。",
                        self.config.system_audio_device)

    def _worker_loop(self) -> None:
        while True:
            chunk = self._q.get()
            if chunk is None:
                break
            try:
                for seg in self._tx.transcribe(str(chunk.wav_path)):
                    ts_s = chunk.ts_start + timedelta(seconds=seg.start)
                    ts_e = chunk.ts_start + timedelta(seconds=seg.end)
                    self._db.insert_transcript(
                        self.session_id, ts_s.isoformat(), ts_e.isoformat(),
                        chunk.speaker, _scrub_text(seg.text), seg.avg_logprob,
                    )
                    self.transcript_count += 1
            except Exception as e:  # pragma: no cover
                logger.warning("转写失败 {}: {}", chunk.wav_path, e)

    def start(self) -> bool:
        if not self.config.audio_enabled:
            return False
        if not self._tx.available:
            logger.warning("Whisper 不可用，音频会话不启动。")
            return False
        self._db.connect()
        self._build_tracks()
        started = [t for t in self._tracks if t.start()]
        if not started:
            logger.warning("没有可用音频轨，音频会话未启动。")
            self._db.close()
            return False
        self._tracks = started
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()
        logger.info("🎙 音频会话启动：{} 轨（{}）", len(started),
                    ", ".join(t.speaker for t in started))
        return True

    def stop(self) -> int:
        for t in self._tracks:
            t.stop()  # flush 尾段 → 入队
        self._q.put(None)
        if self._worker:
            self._worker.join(timeout=120)
        self._db.close()
        logger.info("🎙 音频会话结束，转写 {} 段。", self.transcript_count)
        return self.transcript_count
