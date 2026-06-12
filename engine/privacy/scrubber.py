"""PII 涂码：截图发云端前在本机把含敏感信息的文字区域涂黑。

策略（ARCHITECTURE §5.2，「宁可多涂」）：
1. Apple Vision OCR 拿到每行文字 + 像素 bbox。
2. 正则匹配中文场景常见 PII（手机号、身份证、银行卡、邮箱、IP）。
3. 命中的行 **整行涂黑**（粗粒度但安全），返回涂码后的图 + 涂码清单。

借鉴 openadapt privacy 的分级思路，但这里用「OCR 定位 + 矩形涂黑」而非 Presidio 图像引擎，
因为我们要的是中文远控画面的快速本地涂码。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from PIL import Image, ImageDraw

from engine.config import SunLensConfig
from engine.understand.ocr import OCRLine, ocr_image

# 中文场景 PII 正则（实体类型, 模式）
_PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("PHONE_CN", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("ID_CARD_CN", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("BANK_CARD", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    ("IP", re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")),
]

# 含这些关键词的行，疑似密码/密钥，整行涂黑
_SENSITIVE_KEYWORDS = ["密码", "password", "passwd", "口令", "密钥", "secret", "token", "私钥"]


@dataclass
class Redaction:
    """一处涂码记录。"""

    entity: str
    text_hash: str  # 原文 SHA256 前 16 位（审计用，不留明文）
    left: int
    top: int
    width: int
    height: int


def _line_pii_entities(line: OCRLine) -> list[str]:
    """判断一行文字命中哪些 PII 类型。"""
    hits: list[str] = []
    text = line.text
    low = text.lower()
    for kw in _SENSITIVE_KEYWORDS:
        if kw in low:
            hits.append("SENSITIVE_KEYWORD")
            break
    for entity, pat in _PII_PATTERNS:
        if pat.search(text):
            hits.append(entity)
    return hits


def scrub_image(
    image: Image.Image, config: SunLensConfig
) -> tuple[Image.Image, list[Redaction]]:
    """返回 (涂码后的图, 涂码清单)。redact_enabled=False 时原样返回。"""
    if not config.redact_enabled:
        return image, []

    lines = ocr_image(image, config.ocr_languages)
    redactions: list[Redaction] = []
    out = image.convert("RGB").copy()
    draw = ImageDraw.Draw(out)

    for line in lines:
        entities = _line_pii_entities(line)
        if not entities:
            continue
        # 略微外扩涂黑区域，避免边缘漏字
        pad = 3
        box = (
            max(0, line.left - pad),
            max(0, line.top - pad),
            min(out.width, line.left + line.width + pad),
            min(out.height, line.top + line.height + pad),
        )
        draw.rectangle(box, fill=(0, 0, 0))
        redactions.append(
            Redaction(
                entity="+".join(entities),
                text_hash=hashlib.sha256(line.text.encode("utf-8")).hexdigest()[:16],
                left=box[0],
                top=box[1],
                width=box[2] - box[0],
                height=box[3] - box[1],
            )
        )
    return out, redactions
