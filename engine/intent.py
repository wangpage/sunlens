"""会话级意图理解：从一段操作（动作流 + 输入事件）推理用户的
「任务 / 动机 / 想获取的信息 / 结论」，再做一次**反思自检**（对照证据自我批判、修订），
结果存库供仪表盘「意图摘要」展示。

这一层不再逐帧复述"看到了什么"，而是理解"用户在干嘛、为什么、想要什么"。
不改模型权重——"越用越准"靠**反馈记忆**：用户的纠正存库，下次作为上下文喂回（in-context learning）。
"""

from __future__ import annotations

import json

from loguru import logger

from engine.config import SunLensConfig
from engine.store.db import DB
from engine.studio import _chat
from engine.understand.vlm import _extract_json

_INTENT_SYSTEM = (
    "你是用户行为分析师。给你一段用户在某窗口里的操作记录（按时间的动作 + 输入线索）。"
    "不要逐条复述动作，而要**推理用户在完成什么任务、为什么做、想获取什么信息、最终得到了什么**。"
    "特别注意区分两类行为："
    "①操作型（点设置/传文件/建目录等）——目的在操作本身；"
    "②信息消费型（看视频/读文档/浏览页面）——**目的在他所看的内容里**，这时必须说清"
    "**那段内容的主题是什么、用户想从中获取什么信息**，而不是只说‘在用播放器/在浏览’。"
    "例如‘在播放一个录屏’要进一步问：这录屏录的是什么、用户看它想确认或回顾什么。"
    "若无法判断用户是‘测试功能’还是‘真在消费内容’，在 outcome 里明确点出这个歧义。"
    "严格返回 JSON，字段："
    "task(一句话概括这次的任务)、"
    "why(推断的动机，为什么做这件事)、"
    "info_sought(用户想获取/验证的信息，列点数组；信息消费场景要落到内容本身)、"
    "key_steps(关键步骤摘要，3-6 条数组，合并无意义动作)、"
    "outcome(最终结论/获得了什么；若未观察到完成或存在歧义则说明)。"
    "只依据证据推理，证据不足处不要编造。"
)

_REFLECT_SYSTEM = (
    "你是严格的审稿人。给你「操作证据」和一份「意图分析草稿」。"
    "请自我批判：草稿是否**过度推断**？是否与证据/点击记录**矛盾**？是否**漏掉**了证据指向的信息目标？"
    "尤其检查：是否把‘信息消费’（看视频/读文档）误当成了‘操作工具’而只停在工具层？"
    "是否漏了**所看内容的主题**与**用户想从中获取的信息**？"
    "然后输出修订后的 JSON，字段："
    "task、why、info_sought(数组)、key_steps(数组)、outcome、"
    "confidence(0~1，你对修订结论的把握)、"
    "reflection(中文，说明你改了什么/为什么，或为何维持原判)。"
)


def _digest(db: DB, session_id: str) -> tuple[list[dict], str, str]:
    """把会话的动作流 + 输入线索拼成给模型的证据文本。"""
    sess = db.get_session(session_id) or {}
    title = sess.get("host_window_title", "")
    acts = db.list_action_steps(session_id, limit=500)
    lines = []
    for r in acts:
        seg = f"[{(r['ts'] or '')[11:19]}]"
        if r["type"]:
            seg += f" {r['type']}"
        if r["target_app"]:
            seg += f" @{r['target_app']}"
        if r["nl_description"]:
            seg += f" {r['nl_description']}"
        lines.append(seg)
    inputs = db.list_input_events(session_id)
    typed = "".join(e["key"] for e in inputs
                    if e["kind"] == "key_press" and e.get("key") and len(e["key"]) == 1)
    nclick = sum(1 for e in inputs if e["kind"] == "click")
    meta = f"输入线索：点击 {nclick} 次"
    if typed:
        meta += f"，键入片段：{typed[:80]!r}"
    return acts, title, meta + "\n" + "\n".join(lines)


def _feedback_context(db: DB) -> str:
    """把用户以往的纠正作为上下文（反馈记忆：越用越准，不改权重）。"""
    fb = db.recent_feedback(5)
    if not fb:
        return ""
    return "\n\n用户以往对类似理解的纠正（请吸取，避免重犯）：\n" + "\n".join(
        f"- {f['correction']}" for f in fb
    )


def analyze_intent(config: SunLensConfig, db: DB, session_id: str) -> dict:
    """会话级意图分析（草稿 → 反思修订），存库并返回结果 dict。"""
    acts, title, digest = _digest(db, session_id)
    if not acts:
        return {"error": "该会话还没有理解结果，先 `sunlens understand <id>` 生成动作。"}

    fb = _feedback_context(db)
    evidence = f"目标窗口：{title}\n操作证据：\n{digest}"

    logger.info("意图分析：{}，{} 条动作 → 草稿……", session_id, len(acts))
    draft = _extract_json(_chat(config, _INTENT_SYSTEM, evidence + fb))

    logger.info("意图分析：反思自检中……")
    rev = _extract_json(_chat(
        config, _REFLECT_SYSTEM,
        f"{evidence}\n\n意图分析草稿(JSON)：\n{json.dumps(draft, ensure_ascii=False)}{fb}",
    ))

    final = rev or draft
    for k in ("task", "why", "info_sought", "key_steps", "outcome"):
        if not final.get(k):
            final[k] = draft.get(k)
    final.setdefault("confidence", None)
    final.setdefault("reflection", "")

    db.upsert_intent(session_id, json.dumps(final, ensure_ascii=False))
    logger.info("意图分析完成：{}（confidence={}）", session_id, final.get("confidence"))
    return final
