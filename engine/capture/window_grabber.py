"""单窗口抓帧（Windows）。

首选 `PrintWindow`（ctypes GDI）直接抓**窗口自身内容**——即使被遮挡、在后台、
非最前台也能抓到它真实画面；Chrome 等 GPU 渲染窗口需带 `PW_RENDERFULLCONTENT`。
失败或抓到全黑时，回退到 `PIL.ImageGrab`（截屏幕矩形，要求窗口在屏可见）。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from loguru import logger
from PIL import Image, ImageGrab

from engine.capture.detector import WindowInfo

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

# ---- 常量 ----
_PW_RENDERFULLCONTENT = 0x00000002  # 抓 GPU 渲染/DirectComposition 内容(Chrome 等必需)
_BI_RGB = 0
_DIB_RGB_COLORS = 0

# ---- 64 位句柄必须显式声明，否则默认 c_int 会截断成废句柄 ----
_user32.GetWindowDC.restype = wintypes.HDC
_user32.GetWindowDC.argtypes = [wintypes.HWND]
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.PrintWindow.restype = wintypes.BOOL
_user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
_gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [wintypes.HDC]
_gdi32.GetDIBits.restype = ctypes.c_int
_gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


def bring_to_front(window_id: int) -> None:
    """尽力把窗口置前（best-effort）。PrintWindow 抓取已不依赖置前，仅备用。"""
    try:
        _user32.ShowWindow(window_id, 9)  # SW_RESTORE
        _user32.SetForegroundWindow(window_id)
    except Exception:
        pass


def _capture_printwindow(hwnd: int, w: int, h: int) -> Image.Image | None:
    """用 PrintWindow 抓窗口自身内容；失败/全黑返回 None。"""
    if w <= 0 or h <= 0:
        return None
    win_dc = _user32.GetWindowDC(hwnd)
    if not win_dc:
        return None
    mem_dc = _gdi32.CreateCompatibleDC(win_dc)
    bitmap = _gdi32.CreateCompatibleBitmap(win_dc, w, h)
    old = _gdi32.SelectObject(mem_dc, bitmap)
    try:
        ok = _user32.PrintWindow(hwnd, mem_dc, _PW_RENDERFULLCONTENT)
        if not ok:
            return None

        hdr = _BITMAPINFOHEADER()
        hdr.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        hdr.biWidth = w
        hdr.biHeight = -h  # 负=自上而下，行序与 PIL 一致
        hdr.biPlanes = 1
        hdr.biBitCount = 32
        hdr.biCompression = _BI_RGB

        buf = ctypes.create_string_buffer(w * h * 4)
        _gdi32.SelectObject(mem_dc, old)  # 取出位图后再 GetDIBits（文档要求不被选中）
        got = _gdi32.GetDIBits(win_dc, bitmap, 0, h, buf, ctypes.byref(hdr), _DIB_RGB_COLORS)
        if got == 0:
            return None

        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
        if img.getbbox() is None:  # 全黑(部分 app PrintWindow 失败的表现) → 让上层回退
            return None
        return img
    finally:
        _gdi32.DeleteObject(bitmap)
        _gdi32.DeleteDC(mem_dc)
        _user32.ReleaseDC(hwnd, win_dc)


def capture_window(win: WindowInfo) -> Image.Image:
    """抓取单个窗口的画面，返回 PIL.Image（RGB）。

    优先 PrintWindow（抓窗口自身内容，不怕遮挡）；不行再回退 ImageGrab（截屏幕矩形）。
    """
    img = _capture_printwindow(win.window_id, win.width, win.height)
    if img is not None:
        return img

    logger.debug("PrintWindow 失败/全黑，回退 ImageGrab（需窗口在屏可见）。")
    bbox = (win.x, win.y, win.x + win.width, win.y + win.height)
    grab = ImageGrab.grab(bbox=bbox, all_screens=True)
    if grab.size[0] == 0 or grab.size[1] == 0:
        raise RuntimeError("截到空图像（窗口可能已关闭或不在屏内）。")
    return grab.convert("RGB")
