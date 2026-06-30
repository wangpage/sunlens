"""理解后端：本地 Ollama 上的 qwen3-vl（OpenAI 兼容端点）。

直接吃图（多模态），返回结构化理解。画面不出本机——发往 http://localhost:11434/v1，
无需 API Key、无需脱敏。payload 与 OpenAI/DashScope 同构。
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from io import BytesIO

import requests
from loguru import logger
from PIL import Image

from engine.config import SunLensConfig

_SYSTEM_PROMPT_ZH = (
    "你是一个屏幕操作分析助手。给你一张当前正在操作的窗口截图（如 fnOS NAS 网页会话）、"
    "这一刻前后的鼠标键盘输入事件，以及此前已发生的步骤作为上下文。"
    "请结合上下文判断这一步在做什么——不只是描述像素，而要说清**这一步是为了达成什么**。"
    "若画面是在**看视频/读文档/浏览内容**（信息消费），不要只说‘在用播放器/在浏览’，"
    "而要**读出所看内容的主题**、并推断**用户想从中获取什么信息**。"
    "只依据可见信息和给定线索，不要编造。"
    "严格返回 JSON，字段："
    "description(这一步在做什么、为达成什么，中文一句话)、"
    "intent(这一步的目的，中文)、"
    "app(当前应用/页面名)、"
    "action_type(动作类型，从 click/input/search/open/switch/other 选一个)、"
    "target(操作目标，如按钮名/输入框/链接/文件，中文)、"
    "search_query(若在搜索则填搜索词，否则空串)。"
)


@dataclass
class FrameContext:
    """喂给理解后端的一帧上下文（除截图外的元数据）。"""

    app_window_title: str = ""
    click_xy: tuple[int, int] | None = None
    timestamp: str = ""
    recent_inputs: list[str] = field(default_factory=list)  # 这一帧前后你的输入
    narration: str = ""  # 同时段对方说的话（如有转写）
    history: list[str] = field(default_factory=list)  # 此前已发生的步骤（叙事上下文）


@dataclass
class Understanding:
    """理解后端的产出。"""

    description: str  # 对方在干什么（自然语言）
    intent: str = ""
    app: str = ""
    search_query: str = ""
    action_type: str = ""  # click/input/search/open/switch/other
    target: str = ""  # 操作目标(按钮/元素/文本)
    raw: dict | None = None


def _image_to_data_url(image: Image.Image, config: SunLensConfig) -> str:
    """PIL → 压缩 JPEG → data URL（省内存/token）。"""
    img = image.convert("RGB")
    long_edge = config.max_image_long_edge
    if long_edge and max(img.size) > long_edge:
        scale = long_edge / max(img.size)
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=config.jpeg_quality)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"


def _extract_json(text: str) -> dict:
    """从模型回复里抠出 JSON（容忍 ```json 包裹或前后多余文字）。"""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        candidate = brace.group(0) if brace else None
    if candidate is None:
        return {}
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return {}


def describe_frame(
    config: SunLensConfig, image: Image.Image, context: FrameContext
) -> Understanding:
    """看懂一帧，返回结构化理解（调用本地 Ollama qwen3-vl）。"""
    data_url = _image_to_data_url(image, config)

    user_text = "请分析这张窗口截图。"
    if context.app_window_title:
        user_text += f"\n目标窗口标题：{context.app_window_title}"
    if context.history:
        user_text += "\n此前已发生的步骤：\n- " + "\n- ".join(context.history)
    if context.recent_inputs:
        user_text += "\n这一刻前后的输入事件：" + "；".join(context.recent_inputs)
    if context.narration:
        user_text += f"\n同时段的语音转写：「{context.narration}」"
    if context.click_xy:
        user_text += f"\n最近点击坐标（窗口内）：{context.click_xy}"

    payload = {
        "model": config.vlm_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT_ZH},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
    }
    url = config.ollama_base_url.rstrip("/") + "/chat/completions"
    resp = requests.post(url, json=payload, timeout=config.request_timeout_secs)
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama 调用失败 [{resp.status_code}]: {resp.text[:500]}")

    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    if isinstance(content, list):  # 兼容部分返回为分块结构
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    logger.debug("qwen3-vl 原始回复: {}", content)

    parsed = _extract_json(content)
    return Understanding(
        description=parsed.get("description") or content.strip(),
        intent=parsed.get("intent", ""),
        app=parsed.get("app", ""),
        search_query=parsed.get("search_query", ""),
        action_type=parsed.get("action_type", ""),
        target=parsed.get("target", ""),
        raw=data,
    )
