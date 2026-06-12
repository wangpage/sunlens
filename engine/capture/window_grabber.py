"""单窗口抓帧（macOS / Quartz）。

用 CGWindowListCreateImage 按 window_id 只截那一个窗口，得到 PIL.Image。
只需「屏幕录制」权限；不依赖窗口在最前。

注：M0 用 Quartz 直截，简单可靠。后续 M1 实时高频抓帧若需更低开销，
再换 ScreenCaptureKit（见 ARCHITECTURE §5.1）。
"""

from __future__ import annotations

from PIL import Image

try:
    import Quartz  # type: ignore

    _HAS_QUARTZ = True
except Exception:  # pragma: no cover
    _HAS_QUARTZ = False


def _cgimage_to_pil(cg_image) -> Image.Image:
    """把 CGImage 转成 PIL.Image（RGB）。"""
    width = Quartz.CGImageGetWidth(cg_image)
    height = Quartz.CGImageGetHeight(cg_image)
    if width == 0 or height == 0:
        raise RuntimeError("截到空图像（窗口可能已关闭或无权限）。")

    bytes_per_row = Quartz.CGImageGetBytesPerRow(cg_image)
    provider = Quartz.CGImageGetDataProvider(cg_image)
    data = Quartz.CGDataProviderCopyData(provider)
    buf = bytes(data)

    # CGImage 截屏通常是 BGRA、premultiplied；用 raw 解码器并按实际 stride 读取
    img = Image.frombuffer(
        "RGBA",
        (width, height),
        buf,
        "raw",
        "BGRA",
        bytes_per_row,
        1,
    )
    return img.convert("RGB")


def capture_window(window_id: int) -> Image.Image:
    """按 CGWindowID 截取单个窗口，返回 PIL.Image。"""
    if not _HAS_QUARTZ:
        raise RuntimeError("Quartz 不可用：本工具仅支持 macOS。")

    cg_image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,
        Quartz.kCGWindowListOptionIncludingWindow,
        window_id,
        Quartz.kCGWindowImageBoundsIgnoreFraming
        | Quartz.kCGWindowImageNominalResolution,
    )
    if cg_image is None:
        raise RuntimeError(
            f"无法截取窗口 {window_id}：请确认已授予「屏幕录制」权限，且窗口仍存在。"
        )
    return _cgimage_to_pil(cg_image)
