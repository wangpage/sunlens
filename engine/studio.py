"""Studio：把一次会话的理解结果（动作流）转成可复用的知识产物。

目前一种产物：报告 / SOP —— 用本地 Ollama 把动作流提炼成 Markdown 操作手册，
存进 skill_manual 表，供仪表盘"技能手册"栏回看。每次只需 1 次模型调用。
"""

from __future__ import annotations

import requests
from loguru import logger

from engine.config import SunLensConfig
from engine.store.db import DB

_SOP_SYSTEM = (
    "你是一个把屏幕操作记录整理成可复用《操作手册 / SOP》的助手。"
    "给你一段按时间排序的操作步骤（每条含时间、动作类型、目标应用/页面、自然语言描述）。"
    "请提炼成结构清晰的中文 Markdown 操作手册，包含："
    "一级标题；## 目标（一句话说明这次在做什么）；## 前置条件；"
    "## 操作步骤（有序列表，合并重复/无意义的动作，每步写清在哪个界面点了什么、输入了什么）；"
    "## 注意事项。只依据给定记录，不要编造记录里没有的步骤。"
)


def _session_digest(db: DB, session_id: str, limit: int = 200) -> tuple[list[dict], str]:
    """把会话的 ActionStep 拼成给模型看的纯文本摘要。"""
    rows = db.list_action_steps(session_id, limit=limit)
    lines: list[str] = []
    for r in rows:
        seg = f"[{(r['ts'] or '')[11:19]}]"
        if r["type"]:
            seg += f" {r['type']}"
        if r["target_app"]:
            seg += f" @{r['target_app']}"
        if r["target_text"]:
            seg += f" 目标:{r['target_text']}"
        if r["nl_description"]:
            seg += f" — {r['nl_description']}"
        lines.append(seg)
    return rows, "\n".join(lines)


def _chat(config: SunLensConfig, system: str, user: str) -> str:
    """调本地 Ollama 做一次纯文本对话，返回回复文本。"""
    payload = {
        "model": config.vlm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    url = config.ollama_base_url.rstrip("/") + "/chat/completions"
    resp = requests.post(url, json=payload, timeout=config.request_timeout_secs)
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama 调用失败 [{resp.status_code}]: {resp.text[:300]}")
    content = resp.json()["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content.strip()


def generate_sop(config: SunLensConfig, db: DB, session_id: str) -> dict:
    """把一次会话的动作流总结成 SOP，存库并返回 {type, title, manual_id, data}。"""
    sess = db.get_session(session_id)
    if sess is None:
        raise ValueError(f"未知会话: {session_id}")
    rows, digest = _session_digest(db, session_id)
    if not rows:
        return {"type": "error",
                "error": "该会话还没有理解结果，先 `sunlens understand <id>` 生成动作。"}

    title = sess.get("host_window_title") or session_id
    logger.info("生成 SOP：会话 {}，{} 条动作 → 本地模型……", session_id, len(rows))
    md = _chat(config, _SOP_SYSTEM, f"目标窗口：{title}\n操作记录：\n{digest}")

    # 取首个 Markdown 标题作为手册名
    mtitle = next((ln.lstrip("# ").strip() for ln in md.splitlines() if ln.startswith("#")),
                  f"SOP - {title[:30]}")
    db.delete_session_manuals(session_id, "sop")  # 同会话只保留最新一份，避免重复堆积
    manual_id = db.insert_manual(session_id, mtitle, "sop", md, None)
    return {"type": "markdown", "title": mtitle, "manual_id": manual_id, "data": md}
