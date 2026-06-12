"""插件共享基建：上下文、会话摘要、Qwen 文本调用、JSON 抽取。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import requests

from engine.config import SunLensConfig
from engine.store.db import DB


@dataclass
class Ctx:
    """插件运行上下文。来源 = 勾选的会话 + 勾选的技能手册，聚合生成。"""

    config: SunLensConfig
    db: DB
    session_ids: list[str]
    manual_ids: list[int] = field(default_factory=list)

    @property
    def session_id(self) -> str | None:
        """单会话兼容入口（取第一个，没勾会话则 None）。"""
        return self.session_ids[0] if self.session_ids else None

    def session(self) -> dict:
        s = self.db.get_session(self.session_ids[0])
        if not s:
            raise ValueError(f"未知会话: {self.session_ids[0]}")
        return s

    def _agg(self, fn) -> list[dict]:
        out: list[dict] = []
        for sid in self.session_ids:
            out += fn(sid)
        out.sort(key=lambda r: r.get("ts") or r.get("ts_start") or "")
        return out

    def actions(self) -> list[dict]:
        return self._agg(self.db.list_action_steps)

    def transcripts(self) -> list[dict]:
        return self._agg(lambda sid: self.db.list_transcripts(sid))

    def frames(self) -> list[dict]:
        return self._agg(lambda sid: self.db.list_frames(sid, limit=2000))

    def digest(self) -> str:
        """把勾选的会话压成一段喂给 LLM 的文字记录（多会话分段标题）。"""
        parts: list[str] = []
        for sid in self.session_ids:
            s = self.db.get_session(sid)
            parts.append(f"# 会话：{(s or {}).get('host_window_title','') or sid}")
            acts = self.db.list_action_steps(sid)
            if acts:
                for a in acts:
                    seg = f"- [{a['type'] or '操作'}] "
                    if a["target_app"]:
                        seg += f"{a['target_app']}："
                    seg += a["nl_description"] or ""
                    if a["target_text"]:
                        seg += f"（目标：{a['target_text']}）"
                    if a["narration"]:
                        seg += f"  对方说：{a['narration']}"
                    parts.append(seg)
            else:  # 还没理解，退化用转写
                for t in self.db.list_transcripts(sid):
                    parts.append(f"- {t['speaker']}：{t['text']}")
        # 勾选的技能手册也作为来源喂进去
        for mid in self.manual_ids:
            m = self.db.get_manual(mid)
            if m:
                parts.append(f"# 已有技能手册：{m['title']}\n{m['content']}")
        return "\n".join(parts)


def extract_json(text: str):
    """从模型回复里抠 JSON（容忍 ``` 包裹）。"""
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    cand = m.group(1) if m else text
    m2 = re.search(r"(\{.*\}|\[.*\])", cand, re.DOTALL)
    if not m2:
        return None
    try:
        return json.loads(m2.group(1))
    except json.JSONDecodeError:
        return None


def qwen_text(config: SunLensConfig, system: str, user: str, *, model: str | None = None) -> str:
    """调 Qwen 文本模型（DashScope OpenAI 兼容），返回正文。"""
    if not config.dashscope_api_key:
        raise RuntimeError("缺少 DASHSCOPE_API_KEY，该产出需要 Qwen 文本模型。")
    payload = {
        "model": model or config.qwen_text_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {
        "Authorization": f"Bearer {config.dashscope_api_key}",
        "Content-Type": "application/json",
    }
    url = config.dashscope_base_url.rstrip("/") + "/chat/completions"
    resp = requests.post(url, headers=headers, json=payload, timeout=config.request_timeout_secs)
    if resp.status_code != 200:
        raise RuntimeError(f"Qwen 调用失败 [{resp.status_code}]: {resp.text[:300]}")
    content = resp.json()["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content.strip()
