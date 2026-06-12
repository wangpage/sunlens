"""文生图：通义万相（DashScope 原生，异步任务制）。

流程：创建任务(带 X-DashScope-Async: enable) → 拿 task_id → 轮询 /tasks/{id}
直到 SUCCEEDED → 返回图片 URL（24h 有效）。
"""

from __future__ import annotations

import time

import requests

from engine.config import SunLensConfig


def generate_image(config: SunLensConfig, prompt: str) -> list[str]:
    """文生图，返回图片 URL 列表。"""
    if not config.dashscope_api_key:
        raise RuntimeError("缺少 DASHSCOPE_API_KEY。")

    base = config.dashscope_native_base.rstrip("/")
    headers = {
        "Authorization": f"Bearer {config.dashscope_api_key}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }
    create_body = {
        "model": config.wanx_model,
        "input": {"prompt": prompt},
        "parameters": {"n": 1, "size": config.image_size},
    }
    r = requests.post(
        f"{base}/services/aigc/text2image/image-synthesis",
        headers=headers, json=create_body, timeout=config.request_timeout_secs,
    )
    if r.status_code != 200:
        raise RuntimeError(f"创建文生图任务失败 [{r.status_code}]: {r.text[:400]}")
    task_id = r.json()["output"]["task_id"]

    # 轮询
    poll_headers = {"Authorization": f"Bearer {config.dashscope_api_key}"}
    deadline = time.monotonic() + config.image_poll_timeout_secs
    while time.monotonic() < deadline:
        q = requests.get(f"{base}/tasks/{task_id}", headers=poll_headers,
                         timeout=config.request_timeout_secs)
        out = q.json().get("output", {})
        status = out.get("task_status")
        if status == "SUCCEEDED":
            return [item["url"] for item in out.get("results", []) if "url" in item]
        if status == "FAILED":
            raise RuntimeError(f"文生图任务失败: {out.get('message', '未知错误')}")
        time.sleep(2.0)
    raise RuntimeError("文生图任务超时。")
