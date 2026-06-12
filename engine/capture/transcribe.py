"""本地 Whisper 转写（faster-whisper / CTranslate2）。

模型首次使用时下载并缓存（默认 small，中文够用）。转写在本机跑，**音频不出本机**。
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

try:
    from faster_whisper import WhisperModel

    _HAS_WHISPER = True
except Exception:  # pragma: no cover
    _HAS_WHISPER = False


@dataclass
class Segment:
    start: float  # 相对该段起点的秒数
    end: float
    text: str
    avg_logprob: float | None = None


class Transcriber:
    """懒加载的 faster-whisper 封装。"""

    def __init__(self, model: str = "small", device: str = "cpu",
                 compute_type: str = "int8", language: str = "zh") -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self._model: "WhisperModel | None" = None

    @property
    def available(self) -> bool:
        return _HAS_WHISPER

    def _ensure(self) -> bool:
        if not _HAS_WHISPER:
            logger.warning("faster-whisper 不可用，转写关闭。")
            return False
        if self._model is None:
            logger.info("加载 Whisper 模型 {}（首次会下载）……", self.model_name)
            self._model = WhisperModel(self.model_name, device=self.device,
                                       compute_type=self.compute_type)
        return True

    def transcribe(self, wav_path: str) -> list[Segment]:
        """转写一个 WAV，返回带相对时间戳的片段。"""
        if not self._ensure():
            return []
        segments, _info = self._model.transcribe(
            wav_path, language=self.language, vad_filter=True,
        )
        out: list[Segment] = []
        for s in segments:
            text = (s.text or "").strip()
            if text:
                out.append(Segment(s.start, s.end, text, getattr(s, "avg_logprob", None)))
        return out
