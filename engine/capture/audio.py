"""音频采集（sounddevice / PortAudio）。

按设备分轨采集 → 每 chunk_secs 秒切一段 mono 16k WAV → 回调交给转写。
设备角色即说话人：麦克风=你，loopback(OrayVirtualAudioDevice)=对方。

best-effort：缺 sounddevice / 无麦克风权限 / 设备不存在 → 优雅降级，不报错。
"""

from __future__ import annotations

import threading
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import numpy as np
from loguru import logger

try:
    import sounddevice as sd

    _HAS_SD = True
except Exception:  # pragma: no cover
    _HAS_SD = False


@dataclass
class AudioChunk:
    """一段切好的音频。"""

    wav_path: Path
    speaker: str
    ts_start: datetime  # 墙钟，UTC
    ts_end: datetime


def list_input_devices() -> list[dict]:
    """列出可用输入设备。"""
    if not _HAS_SD:
        return []
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            out.append({"index": i, "name": d["name"],
                        "channels": d["max_input_channels"],
                        "samplerate": int(d["default_samplerate"])})
    return out


def find_device(name_substr: str | None) -> int | None:
    """按名字子串找输入设备索引；name_substr=None 返回 None(用默认输入)。"""
    if not name_substr or not _HAS_SD:
        return None
    low = name_substr.lower()
    for d in list_input_devices():
        if low in d["name"].lower():
            return d["index"]
    return None


def _write_wav(path: Path, frames: np.ndarray, samplerate: int) -> None:
    """float32 [-1,1] → 16-bit PCM mono WAV。"""
    pcm = np.clip(frames, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(samplerate)
        w.writeframes(pcm.tobytes())


class AudioTrack:
    """单设备单轨采集，按 chunk 切段回调。"""

    def __init__(self, *, device: int | None, speaker: str, samplerate: int,
                 chunk_secs: float, out_dir: Path, on_chunk: Callable[[AudioChunk], None]) -> None:
        self.device = device
        self.speaker = speaker
        self.samplerate = samplerate
        self.chunk_secs = chunk_secs
        self.out_dir = out_dir
        self.on_chunk = on_chunk
        self._stream: "sd.InputStream | None" = None
        self._buf: list[np.ndarray] = []
        self._buf_frames = 0
        self._chunk_start: datetime | None = None
        self._lock = threading.Lock()
        self._idx = 0

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            logger.debug("音频状态[{}]: {}", self.speaker, status)
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata.reshape(-1)
        with self._lock:
            if self._chunk_start is None:
                self._chunk_start = datetime.now(timezone.utc)
            self._buf.append(mono.copy())
            self._buf_frames += len(mono)
            if self._buf_frames >= int(self.chunk_secs * self.samplerate):
                self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._buf:
            return
        frames = np.concatenate(self._buf)
        ts_start = self._chunk_start or datetime.now(timezone.utc)
        dur = len(frames) / self.samplerate
        ts_end = ts_start + timedelta(seconds=dur)
        self._idx += 1
        path = self.out_dir / f"{self.speaker}_{self._idx:04d}.wav"
        _write_wav(path, frames, self.samplerate)
        self._buf, self._buf_frames, self._chunk_start = [], 0, None
        try:
            self.on_chunk(AudioChunk(path, self.speaker, ts_start, ts_end))
        except Exception as e:  # pragma: no cover
            logger.warning("on_chunk 回调失败: {}", e)

    def start(self) -> bool:
        if not _HAS_SD:
            return False
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            self._stream = sd.InputStream(
                samplerate=self.samplerate, channels=1, device=self.device,
                dtype="float32", callback=self._callback,
            )
            self._stream.start()
            return True
        except Exception as e:
            logger.warning("音频轨[{}]启动失败: {}", self.speaker, e)
            return False

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        with self._lock:
            self._flush_locked()  # 收尾最后不足一段的音频
