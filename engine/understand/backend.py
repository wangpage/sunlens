"""理解后端统一接口（借 openadapt-desktop/backends/protocol.py 的 Protocol 模式）。

早期：QwenCloudBackend（DashScope 云）。后期：本地 Qwen-VL。
两者实现同一 UnderstandBackend，换引擎不动其它层（ARCHITECTURE §5.3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from PIL import Image


@dataclass
class FrameContext:
    """喂给理解后端的一帧上下文（除截图外的元数据）。"""

    app_window_title: str = ""
    click_xy: tuple[int, int] | None = None
    timestamp: str = ""
    recent_inputs: list[str] = field(default_factory=list)  # 这一帧前后你的输入(M3)
    narration: str = ""  # 同时段对方说的话(M3，来自语音转写)
    history: list[str] = field(default_factory=list)  # RAG 检索到的历史片段（M5+）


@dataclass
class Understanding:
    """理解后端的产出。"""

    description: str  # 对方在干什么（自然语言）
    intent: str = ""  # 推断意图
    app: str = ""  # 当前应用
    search_query: str = ""  # 若在搜索，搜了什么
    action_type: str = ""  # click/input/search/open/switch/other（M3）
    target: str = ""  # 操作目标(按钮/元素/文本)（M3）
    raw: dict | None = None  # 原始返回（调试用）


@runtime_checkable
class UnderstandBackend(Protocol):
    """理解后端协议。"""

    name: str

    def describe_frame(self, image: Image.Image, context: FrameContext) -> Understanding:
        """看懂一帧，返回结构化理解。"""
        ...
