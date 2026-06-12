"""SunLens 配置。

借鉴 openadapt-desktop/engine/config.py 的 Pydantic Settings 模式。
- 普通字段用 SUNLENS_ 前缀环境变量覆盖（如 SUNLENS_QWEN_MODEL）。
- API Key 走原生 DASHSCOPE_API_KEY（不加前缀，不硬编码进代码）。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SunLensConfig(BaseSettings):
    """SunLens 引擎配置。"""

    model_config = SettingsConfigDict(
        env_prefix="SUNLENS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- DashScope / Qwen-VL（国内站）----
    dashscope_api_key: str | None = Field(
        default=None,
        # 同时接受 DASHSCOPE_API_KEY 和 SUNLENS_DASHSCOPE_API_KEY
        validation_alias=AliasChoices("DASHSCOPE_API_KEY", "SUNLENS_DASHSCOPE_API_KEY"),
        description="DashScope API Key。从环境变量读取，绝不写进代码。",
    )
    dashscope_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        description="DashScope OpenAI 兼容端点（国内站）。",
    )
    qwen_model: str = Field(default="qwen-vl-max", description="使用的 Qwen-VL 模型。")
    request_timeout_secs: float = Field(default=60.0)

    # ---- 提问区助手（复用同一 DashScope Key）----
    dashscope_native_base: str = Field(
        default="https://dashscope.aliyuncs.com/api/v1",
        description="DashScope 原生端点（文生图等任务制接口用，国内站）。",
    )
    qwen_text_model: str = Field(default="qwen-plus", description="全网搜索用的文本模型。")
    qwen_research_model: str = Field(default="qwen-max", description="深度研究用的更强模型。")
    wanx_model: str = Field(default="wan2.2-t2i-flash", description="通义万相文生图模型。")
    image_size: str = Field(default="1024*1024", description="文生图尺寸。")
    image_poll_timeout_secs: float = Field(default=60.0, description="文生图任务轮询上限。")
    tts_voice: str | None = Field(
        default=None, description="音频概览 macOS say 语音名；中文需系统装中文语音(如 Tingting)。None=默认。"
    )

    # ---- 向日葵窗口识别（可配置匹配规则，避免改版失效）----
    sunlogin_process_names: list[str] = Field(
        # 向日葵 mac 客户端实测进程名为 AweSun（公司 Oray/贝锐）；
        # SunloginClient 为旧版/Windows 名，一并保留。
        default_factory=lambda: ["AweSun", "Oray", "SunloginClient", "向日葵", "Sunlogin"],
        description="向日葵进程名匹配（不区分大小写，子串匹配）。",
    )
    sunlogin_window_owner_patterns: list[str] = Field(
        default_factory=lambda: ["awesun", "向日葵", "oray", "sunlogin"],
        description="向日葵窗口 owner 名匹配（不区分大小写，子串匹配）。",
    )
    # 远控会话窗口通常比主控制台大；用最小尺寸过滤掉登录/工具小窗
    remote_window_min_width: int = Field(default=480)
    remote_window_min_height: int = Field(default=360)

    # ---- 截图 / 上云压缩 ----
    jpeg_quality: int = Field(default=80, description="发云端前 JPEG 质量。")
    max_image_long_edge: int = Field(
        default=1600,
        description="发云端前把长边压到不超过此像素，省带宽和 token。0=不压缩。",
    )

    # ---- OCR / 脱敏 ----
    ocr_languages: list[str] = Field(default_factory=lambda: ["zh-Hans", "en"])
    redact_enabled: bool = Field(default=True, description="发云端前是否做 PII 涂码。")

    # ---- 理解 ----
    narration_language: str = Field(default="zh", description="解说语言。")

    # ---- 语义记忆 / RAG（DashScope embedding，同一个 Key）----
    memory_enabled: bool = Field(default=True, description="是否把动作/转写/手册向量化用于语义检索。")
    embed_model: str = Field(default="text-embedding-v3", description="DashScope 文本嵌入模型。")
    embed_dim: int = Field(default=1024, description="嵌入维度。")
    rag_top_k: int = Field(default=8, description="语义检索返回条数。")
    rag_augment_k: int = Field(default=3, description="理解时检索多少条相似历史增强。")

    # ---- 行为 ----
    auto_start: bool = Field(default=True, description="检测到远控窗口自动开录（M1+）。")

    # ---- 录制（M1，事件驱动抓帧）----
    poll_interval_secs: float = Field(default=0.5, description="抓帧轮询间隔。")
    min_capture_interval_secs: float = Field(default=0.3, description="两次落帧最小间隔(防抖)。")
    idle_capture_interval_secs: float = Field(
        default=30.0, description="画面没变也每隔这么久强制落一帧(时间线连续)。"
    )
    frame_diff_threshold: float = Field(
        default=0.10, description="aHash 汉明距离/64 超过此值算画面有变化(0~1)。"
    )
    window_lost_grace_secs: float = Field(
        default=3.0, description="远控窗口消失超过这么久就结束会话。"
    )
    capture_input_events: bool = Field(default=True, description="是否捕获你的鼠标/键盘事件。")

    # ---- 音频 + 本地 Whisper 转写（M2）----
    audio_enabled: bool = Field(default=True, description="录制时是否同时采音+转写。")
    mic_device: str | None = Field(
        default=None, description="麦克风设备名(子串匹配)；None=系统默认输入。说话人=你。"
    )
    system_audio_device: str | None = Field(
        default="OrayVirtualAudioDevice",
        description="系统/远端音频 loopback 设备名(子串匹配)；找不到则只录麦克风。说话人=对方。",
    )
    audio_samplerate: int = Field(default=16000, description="采样率(Whisper 要 16k)。")
    audio_chunk_secs: float = Field(default=8.0, description="每多少秒切一段去转写。")
    whisper_model: str = Field(default="small", description="faster-whisper 模型(tiny/base/small/medium)。")
    whisper_compute_type: str = Field(default="int8", description="int8 省内存、CPU 友好。")
    whisper_device: str = Field(default="cpu", description="faster-whisper 设备(cpu)。")
    whisper_language: str = Field(default="zh", description="转写语言。")

    # ---- 路径 ----
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".sunlens")
    log_level: str = Field(default="INFO")


def load_config() -> SunLensConfig:
    """加载配置（自动从环境变量 / .env 读取）。"""
    return SunLensConfig()
