"""9 个 Studio 插件 + 注册表。每个插件 (Ctx) -> result dict。

result.type 决定前端怎么渲染：
  table / markdown / mermaid / cards / quiz / html / audio / slides / deck
"""

from __future__ import annotations

import subprocess

from loguru import logger

from engine.studio.base import Ctx, extract_json, qwen_text


# ----------------------------------------------------------------- 纯数据（无需 key）
def data_table(ctx: Ctx) -> dict:
    rows = [[a["ts"][11:19], a["type"] or "", a["target_app"] or "",
             a["target_text"] or "", a["nl_description"] or "", a["narration"] or ""]
            for a in ctx.actions()]
    return {"type": "table",
            "columns": ["时间", "类型", "应用", "目标", "描述", "对方说"], "rows": rows}


def video_overview(ctx: Ctx) -> dict:
    """帧 + 解说幻灯片（用已理解的描述，无需 key）。"""
    slides = [{"frame_id": a["frame_id"], "caption": a["nl_description"] or a["type"] or "",
               "narration": a["narration"] or ""}
              for a in ctx.actions() if a["frame_id"]]
    if not slides:  # 没理解过，退化用帧
        slides = [{"frame_id": f["id"], "caption": f["trigger"], "narration": ""}
                  for f in ctx.frames()]
    return {"type": "slides", "data": slides}


# ----------------------------------------------------------------- LLM 产出（需 key）
def report_sop(ctx: Ctx) -> dict:
    sys = ("你是把远程操作记录整理成可复用 SOP 的专家。输出 markdown，结构："
           "# 标题\n## 目标\n## 前置条件\n## 步骤（有序，每步写清操作+为什么这么做）\n"
           "## 用到的工具\n## 要点与坑。只依据记录，不编造。")
    md = qwen_text(ctx.config, sys, ctx.digest())
    title = (md.splitlines()[0].lstrip("# ").strip() if md else "技能手册") or "技能手册"
    skills = ctx.config.data_dir / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    token = ("_".join(ctx.session_ids) or "manuals")[:40]
    path = skills / f"{token}_sop.md"
    path.write_text(md, encoding="utf-8")
    # 单会话才挂 session；多会话/含手册合成的不挂
    sid = ctx.session_ids[0] if (len(ctx.session_ids) == 1 and not ctx.manual_ids) else None
    mid = ctx.db.insert_manual(sid, title, "sop", md, str(path))
    # 手册也进语义记忆，可被检索/二次提炼
    if ctx.config.memory_enabled and ctx.config.dashscope_api_key:
        try:
            from engine.memory.index import index_manual
            index_manual(ctx.config, ctx.db, mid)
        except Exception as e:  # pragma: no cover
            logger.warning("索引手册失败: {}", e)
    return {"type": "markdown", "data": md, "manual_id": mid, "title": title}


def mindmap(ctx: Ctx) -> dict:
    sys = ("把以下操作整理成 mermaid mindmap。根节点=任务目标，二级=阶段，三级=具体操作。"
           "只输出 mermaid 代码，以 mindmap 开头，不要 ``` 包裹。")
    data = qwen_text(ctx.config, sys, ctx.digest())
    data = data.replace("```mermaid", "").replace("```", "").strip()
    return {"type": "mermaid", "data": data}


def flashcards(ctx: Ctx) -> dict:
    sys = ("把对方的做法做成学习闪卡。输出 JSON 数组，每项 {front: 情境/问题, back: 对方的做法/答案}，"
           "6-10 张。只输出 JSON。")
    data = extract_json(qwen_text(ctx.config, sys, ctx.digest())) or []
    return {"type": "cards", "data": data}


def quiz(ctx: Ctx) -> dict:
    sys = ("基于记录出 4-6 道测验题检验是否掌握对方方法。输出 JSON 数组，每项 {q: 题目, answer: 参考答案}。"
           "只输出 JSON。")
    data = extract_json(qwen_text(ctx.config, sys, ctx.digest())) or []
    return {"type": "quiz", "data": data}


def infographic(ctx: Ctx) -> dict:
    sys = ("提炼这次会话做信息图。输出 JSON："
           "{summary: 一句话总结, stats: [{label,value}], steps: [关键步骤字符串], tools: [工具]}。只输出 JSON。")
    d = extract_json(qwen_text(ctx.config, sys, ctx.digest())) or {}
    html = _infographic_html(d)
    return {"type": "html", "data": html}


def presentation(ctx: Ctx) -> dict:
    sys = ("把这次会话做成演示文稿大纲。输出 JSON 数组幻灯片，每页 {title, bullets: [要点]}。"
           "封面、目标、关键步骤、总结。只输出 JSON。")
    data = extract_json(qwen_text(ctx.config, sys, ctx.digest())) or []
    return {"type": "deck", "data": data}


def audio_overview(ctx: Ctx) -> dict:
    if not ctx.session_ids:
        raise RuntimeError("音频概览需要至少勾选一个会话。")
    sys = ("把这次会话写成 60-120 秒的中文讲解口播稿，像老师讲课，说清对方做了什么、为什么。只输出口播文字。")
    script = qwen_text(ctx.config, sys, ctx.digest())
    out = ctx.config.data_dir / "sessions" / ctx.session_id / "audio_overview.m4a"
    synthesize_say(script, out, voice=ctx.config.tts_voice)
    return {"type": "audio", "url": f"/api/studio_audio/{ctx.session_id}", "script": script}


# ----------------------------------------------------------------- 辅助
def synthesize_say(text: str, out_m4a, voice: str | None = None) -> bool:
    """macOS say 合成 → m4a。返回是否成功。中文需系统装有中文语音。"""
    out_m4a.parent.mkdir(parents=True, exist_ok=True)
    aiff = out_m4a.with_suffix(".aiff")
    say_cmd = ["say", text, "-o", str(aiff)]
    if voice:
        say_cmd[1:1] = ["-v", voice]
    try:
        subprocess.run(say_cmd, check=True, capture_output=True)
        subprocess.run(["afconvert", str(aiff), str(out_m4a), "-f", "m4af", "-d", "aac"],
                       check=True, capture_output=True)
        aiff.unlink(missing_ok=True)
        return True
    except Exception as e:  # pragma: no cover
        logger.warning("say 合成失败: {}", e)
        return False


def _infographic_html(d: dict) -> str:
    stats = "".join(
        f'<div style="background:#232425;border-radius:10px;padding:12px;text-align:center">'
        f'<div style="font-size:22px;color:#8ab4f8">{s.get("value","")}</div>'
        f'<div style="color:#9aa0a6;font-size:12px">{s.get("label","")}</div></div>'
        for s in d.get("stats", []))
    steps = "".join(f"<li>{s}</li>" for s in d.get("steps", []))
    tools = "、".join(d.get("tools", []))
    return (f'<h2>{d.get("summary","会话信息图")}</h2>'
            f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(90px,1fr));gap:10px;margin:12px 0">{stats}</div>'
            f'<h3>关键步骤</h3><ol>{steps}</ol>'
            f'<h3>用到的工具</h3><p>{tools}</p>')


# ----------------------------------------------------------------- 注册表
PLUGINS: dict[str, dict] = {
    "report":       {"name": "报告 / SOP", "fn": report_sop,      "needs_key": True},
    "data_table":   {"name": "数据表格",   "fn": data_table,      "needs_key": False},
    "video":        {"name": "视频概览",   "fn": video_overview,  "needs_key": False},
    "audio":        {"name": "音频概览",   "fn": audio_overview,  "needs_key": True},
    "mindmap":      {"name": "思维导图",   "fn": mindmap,         "needs_key": True},
    "flashcards":   {"name": "闪卡",       "fn": flashcards,      "needs_key": True},
    "quiz":         {"name": "测验",       "fn": quiz,            "needs_key": True},
    "infographic":  {"name": "信息图",     "fn": infographic,     "needs_key": True},
    "presentation": {"name": "演示文稿",   "fn": presentation,    "needs_key": True},
}


def run_plugin(config, db, plugin_id: str, session_ids: list[str],
               manual_ids: list[int] | None = None) -> dict:
    spec = PLUGINS.get(plugin_id)
    if not spec:
        raise ValueError(f"未知插件: {plugin_id}")
    manual_ids = manual_ids or []
    if not session_ids and not manual_ids:
        raise ValueError("未勾选任何来源")
    result = spec["fn"](Ctx(config, db, session_ids, manual_ids))
    result["plugin"] = plugin_id
    result["name"] = spec["name"]
    return result
