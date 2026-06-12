"""DashScope 文本嵌入（OpenAI 兼容 /embeddings，复用同一个 Key）。"""

from __future__ import annotations

import requests

from engine.config import SunLensConfig

_BATCH = 10  # text-embedding-v3 单次批量上限保守取 10


def embed_texts(config: SunLensConfig, texts: list[str]) -> list[list[float]]:
    """把一批文本转成向量。返回与输入同序的向量列表。"""
    if not config.dashscope_api_key:
        raise RuntimeError("缺少 DASHSCOPE_API_KEY，语义记忆需要嵌入模型。")
    if not texts:
        return []
    headers = {
        "Authorization": f"Bearer {config.dashscope_api_key}",
        "Content-Type": "application/json",
    }
    url = config.dashscope_base_url.rstrip("/") + "/embeddings"
    out: list[list[float]] = []
    for i in range(0, len(texts), _BATCH):
        batch = texts[i:i + _BATCH]
        payload = {
            "model": config.embed_model,
            "input": batch,
            "dimensions": config.embed_dim,
            "encoding_format": "float",
        }
        resp = requests.post(url, headers=headers, json=payload,
                             timeout=config.request_timeout_secs)
        if resp.status_code != 200:
            raise RuntimeError(f"嵌入失败 [{resp.status_code}]: {resp.text[:300]}")
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        out.extend(d["embedding"] for d in data)
    return out
