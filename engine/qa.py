"""接地问答：让本地模型**基于已记录、已理解的记忆**回答用户的问题。

不再是关键词 LIKE 匹配，而是把近期会话（时间/窗口/已分析的意图/动作）汇成"记忆"，
连同当前时间一起喂给本地 32B，让它**归纳、关联、按时间推理**（如"今天下午干了什么"），
用自然语言作答。只依据记录，不编造。
"""

from __future__ import annotations

import json
from datetime import datetime

from loguru import logger

from engine.config import SunLensConfig
from engine.store.db import DB
from engine.studio import _chat

_QA_SYSTEM = (
    "你是用户的‘操作记忆’助手。下面给你用户近期在电脑上被记录并理解过的操作"
    "（按会话列出：时间、窗口、已分析的意图或动作摘要）。"
    "请**基于这些记忆**用自然语言回答用户的问题：可以归纳、关联、按时间推理"
    "（例如‘今天下午’要结合会话时间判断）。"
    "只依据给定记忆作答，不要编造；记忆里确实没有的就如实说‘没有记录到’。回答简洁、直接。"
)


def _memory_digest(db: DB, max_sessions: int = 10) -> str:
    """把近期会话汇成给模型看的‘记忆’文本（优先用意图摘要，否则用动作摘要）。"""
    lines: list[str] = []
    for s in db.list_sessions(limit=max_sessions):
        sid = s["id"]
        when = (s.get("started_at") or "")[:16].replace("T", " ")
        title = s.get("host_window_title") or sid
        seg = f"- [{when}] 窗口《{title}》"
        intent = db.get_intent(sid)
        if intent:
            try:
                d = json.loads(intent["data"])
                seg += f"；意图：{d.get('task', '')}"
                if d.get("outcome"):
                    seg += f"；结果：{d['outcome']}"
            except Exception:
                pass
        else:
            acts = [a["nl_description"] for a in db.list_action_steps(sid, limit=6)
                    if a["nl_description"]]
            if acts:
                seg += "；操作：" + "；".join(acts[:4])
        lines.append(seg)
    return "\n".join(lines)


def answer_question(config: SunLensConfig, db: DB, question: str) -> dict:
    """基于记忆回答问题，返回 {answer}。"""
    digest = _memory_digest(db)
    if not digest:
        return {"answer": "还没有任何录制记忆。先 `sunlens start` 录一段并 `understand` 后再问我。"}

    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M（%A）")
    user = f"当前时间：{now}\n我的操作记忆：\n{digest}\n\n我的问题：{question}"
    logger.info("接地问答：{}", question)
    answer = _chat(config, _QA_SYSTEM, user)
    return {"answer": answer}
