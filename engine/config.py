"""SunLens 配置（Pydantic Settings）。

普通字段用 SUNLENS_ 前缀环境变量覆盖（如 SUNLENS_VLM_MODEL）。
理解走本地 Ollama，无需任何 API Key。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SunLensConfig(BaseSettings):
    """SunLens 引擎配置。"""

    model_config = SettingsConfigDict(
        env_prefix="SUNLENS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 本地 Ollama / qwen3-vl（OpenAI 兼容端点）----
    ollama_base_url: str = Field(
        default="http://localhost:11434/v1",
        description="Ollama OpenAI 兼容端点。",
    )
    # sunlens-vl:32b = qwen3-vl:32b-instruct + num_ctx=8192（见 Modelfile.32b）：
    # 质量远胜 8B，且砍小上下文后能 100% 吃进核显。无此模型时回退 qwen3-vl:8b-instruct。
    vlm_model: str = Field(default="sunlens-vl:32b", description="本地视觉理解模型。")
    # 本地 CPU 推理首帧含模型加载，给足超时；GPU 机器可调小
    request_timeout_secs: float = Field(default=600.0)

    # ---- 目标窗口识别（要实时记录的那个窗口；默认 fnOS NAS 网页会话）----
    # 操作 NAS 时它跑在浏览器标签里，窗口标题形如「prizNAS - 飞牛 fnOS - Chrome」，
    # 故按标题/owner 子串匹配 飞牛/fnos/priznas。可按需改成别的目标窗口标识。
    target_window_patterns: list[str] = Field(
        default_factory=lambda: ["飞牛", "fnos", "priznas"],
        description="目标窗口 owner/标题匹配（不区分大小写，子串匹配）。",
    )
    # 用最小尺寸过滤掉小工具窗/通知；只记录够大的主会话窗口
    target_window_min_width: int = Field(default=480)
    target_window_min_height: int = Field(default=360)

    # ---- 截图压缩（送模型前）----
    jpeg_quality: int = Field(default=80, description="存盘/送模型前 JPEG 质量。")
    max_image_long_edge: int = Field(
        default=1024,
        description="送模型前把长边压到不超过此像素，省内存/token（CPU 推理尤其重要）。0=不压缩。",
    )

    # ---- 理解（关键帧采样：只挑视觉显著变化的帧送模型，省大量算力）----
    narration_language: str = Field(default="zh", description="解说语言。")
    keyframe_diff_threshold: float = Field(
        default=0.12,
        description="某帧 aHash 与上一关键帧的距离/64 超过此值才算关键帧。越大越省、越粗。",
    )
    understand_max_frames: int = Field(
        default=60, description="单会话最多理解多少关键帧（超出则均匀抽样）；0=不限。"
    )

    # ---- 行为 ----
    auto_start: bool = Field(default=True, description="检测到目标窗口自动开录。")

    # ---- 录制（事件驱动抓帧）----
    startup_wait_secs: float = Field(
        default=120.0, description="开录后等待目标窗口出现的上限（给你时间切到 NAS 标签页）。"
    )
    poll_interval_secs: float = Field(default=0.5, description="抓帧轮询间隔。")
    min_capture_interval_secs: float = Field(default=0.3, description="两次落帧最小间隔(防抖)。")
    idle_capture_interval_secs: float = Field(
        default=30.0, description="画面没变也每隔这么久强制落一帧(时间线连续)。"
    )
    frame_diff_threshold: float = Field(
        default=0.10, description="aHash 汉明距离/64 超过此值算画面有变化(0~1)。"
    )
    window_lost_grace_secs: float = Field(
        default=3.0, description="目标窗口消失超过这么久就结束会话。"
    )
    capture_input_events: bool = Field(default=True, description="是否捕获你的鼠标/键盘事件。")

    # ---- 路径 ----
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".sunlens")
    log_level: str = Field(default="INFO")


def load_config() -> SunLensConfig:
    """加载配置（自动从环境变量 / .env 读取）。"""
    return SunLensConfig()
