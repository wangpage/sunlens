"""后端 A：Qwen-VL 云 API（阿里 DashScope，OpenAI 兼容端点）。

借鉴 OpenAdapt drivers/openai.py 的 image_url payload 结构 + utils.image2utf8。
直接吃图（多模态），返回结构化理解。

⚠️ 调用前调用方必须确保图像已脱敏（出口闸门 privacy/gate.py）。
"""

from __future__ import annotations

import base64
import json
import re
from io import BytesIO

import requests
from loguru import logger
from PIL import Image

from engine.config import SunLensConfig
from engine.understand.backend import FrameContext, Understanding

_SYSTEM_PROMPT_ZH = (
    "你是一个屏幕操作分析助手。用户正在通过向日葵远程桌面观察「对方」在另一台电脑上操作。"
    "给你一张远程桌面的截图（可能含被涂黑的隐私区域，忽略黑块），"
    "以及这一刻前后的输入事件和对方说的话作为辅助线索。"
    "请综合判断对方此刻在做什么。只依据可见信息和给定线索，不要编造被涂黑的内容。"
    "严格返回 JSON，字段："
    "description(对方在做什么，中文一句话)、"
    "intent(推断意图，中文)、"
    "app(当前应用名)、"
    "action_type(动作类型，从 click/input/search/open/switch/other 选一个)、"
    "target(操作目标，如按钮名/输入框/链接/文件，中文)、"
    "search_query(若在搜索则填搜索词，否则空串)。"
)


def _image_to_data_url(image: Image.Image, config: SunLensConfig) -> str:
    """PIL → 压缩 JPEG → data URL（省带宽/token）。"""
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


class QwenCloudBackend:
    """DashScope 上的 qwen-vl-max（国内站，OpenAI 兼容）。"""

    name = "qwen-cloud"

    def __init__(self, config: SunLensConfig) -> None:
        self.config = config
        if not config.dashscope_api_key:
            raise RuntimeError(
                "缺少 DASHSCOPE_API_KEY。请 export DASHSCOPE_API_KEY=sk-xxx 后重试。"
            )

    def describe_frame(self, image: Image.Image, context: FrameContext) -> Understanding:
        data_url = _image_to_data_url(image, self.config)

        user_text = "请分析这张远程桌面截图。"
        if context.app_window_title:
            user_text += f"\n向日葵窗口标题：{context.app_window_title}"
        if context.recent_inputs:
            user_text += "\n这一刻前后的输入事件：" + "；".join(context.recent_inputs)
        if context.narration:
            user_text += f"\n对方同时说的话：「{context.narration}」"
        if context.click_xy:
            user_text += f"\n最近点击坐标（窗口内）：{context.click_xy}"
        if context.history:
            user_text += "\n相关历史片段：\n- " + "\n- ".join(context.history)

        payload = {
            "model": self.config.qwen_model,
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
        headers = {
            "Authorization": f"Bearer {self.config.dashscope_api_key}",
            "Content-Type": "application/json",
        }
        url = self.config.dashscope_base_url.rstrip("/") + "/chat/completions"

        resp = requests.post(
            url, headers=headers, json=payload, timeout=self.config.request_timeout_secs
        )
        if resp.status_code != 200:
            raise RuntimeError(f"DashScope 调用失败 [{resp.status_code}]: {resp.text[:500]}")

        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):  # 兼容部分返回为分块结构
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        logger.debug("Qwen-VL 原始回复: {}", content)

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
