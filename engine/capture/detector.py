"""检测向日葵进程与远控窗口（macOS）。

进程：psutil 扫描名字匹配 SunloginClient / 向日葵。
窗口：Quartz CGWindowListCopyWindowInfo 枚举屏上窗口，按 owner 名匹配，
      再用最小尺寸过滤掉登录框/工具小窗，挑出最像「远控会话」的那个。

借鉴 screenpipe 的窗口枚举思路（CGWindowList + owner/bounds），翻成 Python。
"""

from __future__ import annotations

from dataclasses import dataclass

import psutil
from loguru import logger

try:  # 仅 macOS 可用
    import Quartz  # type: ignore

    _HAS_QUARTZ = True
except Exception:  # pragma: no cover - 非 macOS
    _HAS_QUARTZ = False

from engine.config import SunLensConfig


@dataclass
class WindowInfo:
    """一个屏上窗口的信息。"""

    window_id: int
    owner: str
    title: str
    pid: int
    x: int
    y: int
    width: int
    height: int
    layer: int

    @property
    def area(self) -> int:
        return self.width * self.height


def is_sunlogin_running(config: SunLensConfig) -> list[psutil.Process]:
    """返回所有匹配的向日葵进程（可能多个：主程序+守护）。"""
    needles = [n.lower() for n in config.sunlogin_process_names]
    matched: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name"]):
        name = (proc.info.get("name") or "").lower()
        if any(n in name for n in needles):
            matched.append(proc)
    return matched


def list_windows() -> list[WindowInfo]:
    """枚举当前屏上所有窗口。"""
    if not _HAS_QUARTZ:
        raise RuntimeError("Quartz 不可用：本工具仅支持 macOS（需 pyobjc-framework-Quartz）。")

    options = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    raw = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
    windows: list[WindowInfo] = []
    for w in raw or []:
        bounds = w.get("kCGWindowBounds", {})
        windows.append(
            WindowInfo(
                window_id=int(w.get("kCGWindowNumber", 0)),
                owner=str(w.get("kCGWindowOwnerName", "")),
                title=str(w.get("kCGWindowName", "") or ""),
                pid=int(w.get("kCGWindowOwnerPID", 0)),
                x=int(bounds.get("X", 0)),
                y=int(bounds.get("Y", 0)),
                width=int(bounds.get("Width", 0)),
                height=int(bounds.get("Height", 0)),
                layer=int(w.get("kCGWindowLayer", 0)),
            )
        )
    return windows


def find_sunlogin_windows(config: SunLensConfig) -> list[WindowInfo]:
    """找出所有疑似向日葵的窗口（按 owner 名匹配）。"""
    needles = [p.lower() for p in config.sunlogin_window_owner_patterns]
    out: list[WindowInfo] = []
    for win in list_windows():
        hay = f"{win.owner} {win.title}".lower()
        if any(n in hay for n in needles):
            out.append(win)
    return out


def pick_remote_window(config: SunLensConfig) -> WindowInfo | None:
    """从向日葵窗口里挑出最像「远控会话画面」的那个。

    启发式：layer==0（普通窗口）+ 尺寸超过阈值，取面积最大的。
    远控画面通常是最大的那个窗口；登录框/工具条会被尺寸阈值滤掉。
    """
    candidates = [
        w
        for w in find_sunlogin_windows(config)
        if w.layer == 0
        and w.width >= config.remote_window_min_width
        and w.height >= config.remote_window_min_height
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda w: w.area)
    logger.debug(
        "选中远控窗口: id={} owner={!r} title={!r} {}x{}",
        best.window_id,
        best.owner,
        best.title,
        best.width,
        best.height,
    )
    return best
