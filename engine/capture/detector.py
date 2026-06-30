"""检测要实时记录的「目标窗口」（Windows）。

默认目标是 fnOS NAS 的网页会话——它跑在浏览器标签里，窗口标题形如
「prizNAS - 飞牛 fnOS - Chrome」。用 ctypes 调 user32.EnumWindows 枚举顶层窗口，
取标题 + 矩形 + 所属进程名(owner)，按 config.target_window_patterns 子串匹配，
再用最小尺寸过滤掉小工具窗，挑面积最大的那个作为「当前正在操作的目标」。

纯 ctypes，无需 pywin32。owner 通过 GetWindowThreadProcessId + psutil 拿进程名。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

import psutil
from loguru import logger

from engine.config import SunLensConfig

_user32 = ctypes.windll.user32
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


@dataclass
class WindowInfo:
    """一个屏上窗口的信息。"""

    window_id: int  # HWND
    owner: str  # 所属进程名
    title: str
    pid: int
    x: int
    y: int
    width: int
    height: int
    layer: int = 0  # Windows 无 layer 概念，恒 0（保持与上层接口一致）

    @property
    def area(self) -> int:
        return self.width * self.height


def _pid_name_cache() -> dict[int, str]:
    """pid → 进程名，一次性建表，避免逐窗口查询。"""
    out: dict[int, str] = {}
    for proc in psutil.process_iter(["pid", "name"]):
        out[proc.info["pid"]] = proc.info.get("name") or ""
    return out


def list_windows() -> list[WindowInfo]:
    """枚举当前所有可见、带标题的顶层窗口。"""
    pid_names = _pid_name_cache()
    windows: list[WindowInfo] = []

    def _cb(hwnd: int, _lparam: int) -> bool:
        if not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value

        rect = wintypes.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return True

        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        owner = pid_names.get(pid.value, "")
        if not title and not owner:
            return True

        windows.append(
            WindowInfo(
                window_id=int(hwnd),
                owner=owner,
                title=title,
                pid=int(pid.value),
                x=rect.left,
                y=rect.top,
                width=w,
                height=h,
            )
        )
        return True

    _user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    return windows


def find_target_windows(config: SunLensConfig) -> list[WindowInfo]:
    """找出所有疑似目标的窗口（按 owner 进程名 / 标题匹配）。"""
    needles = [p.lower() for p in config.target_window_patterns]
    out: list[WindowInfo] = []
    for win in list_windows():
        hay = f"{win.owner} {win.title}".lower()
        if any(n in hay for n in needles):
            out.append(win)
    return out


def pick_target_window(config: SunLensConfig) -> WindowInfo | None:
    """从匹配窗口里挑出当前正在操作的那个目标窗口。

    启发式：尺寸超过阈值，取面积最大的——正在操作的会话画面通常是最大的窗口，
    小工具窗/通知会被尺寸阈值滤掉。匹配为空（如切到别的标签页）时返回 None，
    上层据此判断「目标已不在前台」并停止记录。
    """
    candidates = [
        w
        for w in find_target_windows(config)
        if w.width >= config.target_window_min_width
        and w.height >= config.target_window_min_height
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda w: w.area)
    logger.debug(
        "选中目标窗口: hwnd={} owner={!r} title={!r} {}x{}",
        best.window_id, best.owner, best.title, best.width, best.height,
    )
    return best
