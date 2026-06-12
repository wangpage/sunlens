"""Apple Vision OCR（macOS 原生，中文好、本地、免费）。

在 SunLens 里 OCR 的主要职责不是「理解」（理解交给 Qwen-VL），而是
**定位画面里的文字及其位置**，好让脱敏层把 PII 区域涂掉（ARCHITECTURE §5.2）。

返回每行：文本 + 像素 bbox（左上原点，单位=像素，与传入图像同尺寸）。
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger
from PIL import Image

try:
    import Quartz  # type: ignore
    import Vision  # type: ignore

    _HAS_VISION = True
except Exception:  # pragma: no cover
    _HAS_VISION = False


@dataclass
class OCRLine:
    """一行 OCR 结果。bbox 为像素坐标 (left, top, width, height)，左上原点。"""

    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float


def _pil_to_cgimage(image: Image.Image):
    """PIL.Image → CGImage（经 PNG 字节，稳妥跨格式）。"""
    from io import BytesIO

    buf = BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    data = buf.getvalue()

    provider = Quartz.CGDataProviderCreateWithCFData(data)
    cg_image = Quartz.CGImageCreateWithPNGDataProvider(
        provider, None, True, Quartz.kCGRenderingIntentDefault
    )
    return cg_image


def ocr_image(image: Image.Image, languages: list[str] | None = None) -> list[OCRLine]:
    """对 PIL 图像做 OCR，返回带像素 bbox 的文本行。"""
    if not _HAS_VISION:
        logger.warning("Vision 框架不可用，OCR 跳过（脱敏将退化为不涂码）。")
        return []

    languages = languages or ["zh-Hans", "en"]
    cg_image = _pil_to_cgimage(image)
    if cg_image is None:
        return []

    w, h = image.size

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setRecognitionLanguages_(languages)
    request.setUsesLanguageCorrection_(True)

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, {})
    success, err = handler.performRequests_error_([request], None)
    if not success:
        logger.warning("OCR 失败: {}", err)
        return []

    lines: list[OCRLine] = []
    for obs in request.results() or []:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        cand = cands[0]
        text = cand.string()
        if not text:
            continue
        # Vision boundingBox：归一化 [0,1]，左下原点 → 转像素、左上原点
        bb = obs.boundingBox()
        bx, by = bb.origin.x, bb.origin.y
        bw, bh = bb.size.width, bb.size.height
        left = int(bx * w)
        top = int((1.0 - by - bh) * h)
        width = int(bw * w)
        height = int(bh * h)
        lines.append(
            OCRLine(
                text=text,
                left=left,
                top=top,
                width=width,
                height=height,
                confidence=float(obs.confidence()),
            )
        )
    return lines
