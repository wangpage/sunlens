"""全网搜索 / 深度研究：qwen 文本模型 + 联网增强（enable_search）。

复用 DashScope OpenAI 兼容端点 /chat/completions，加 enable_search 即开启
Model Studio 服务端联网搜索。深度研究换更强模型 + 更系统的 prompt。
"""

from __future__ import annotations

import requests

from engine.config import SunLensConfig

_RESEARCH_SYS = (
    "你是严谨的研究助手。基于联网搜索给出准确、结构化的回答："
    "先给结论，再分点列出依据与关键事实，最后指出不确定处。中文作答。"
)
_SEARCH_SYS = "你是联网问答助手。基于实时搜索结果简明准确地回答问题，中文作答。"


def web_answer(config: SunLensConfig, query: str, *, deep: bool = False) -> str:
    """联网问答。deep=True 走深度研究（更强模型 + 研究式 prompt）。"""
    if not config.dashscope_api_key:
        raise RuntimeError("缺少 DASHSCOPE_API_KEY。")

    model = config.qwen_research_model if deep else config.qwen_text_model
    system = _RESEARCH_SYS if deep else _SEARCH_SYS
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": query},
        ],
        # DashScope 联网搜索：OpenAI 兼容模式下作为顶层字段透传
        "enable_search": True,
        "search_options": {"search_strategy": "agent"},
    }
    headers = {
        "Authorization": f"Bearer {config.dashscope_api_key}",
        "Content-Type": "application/json",
    }
    url = config.dashscope_base_url.rstrip("/") + "/chat/completions"
    resp = requests.post(url, headers=headers, json=payload,
                         timeout=config.request_timeout_secs * (2 if deep else 1))
    if resp.status_code != 200:
        raise RuntimeError(f"联网问答失败 [{resp.status_code}]: {resp.text[:400]}")
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content.strip()
