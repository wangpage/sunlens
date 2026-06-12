"""主控端鼠标/键盘事件捕获（pynput，借 OpenAdapt record.py 的 Listener 模式）。

你在主控端的输入会转发给对方机器，所以记录你的点击/打字 = 记录"对方机器上发生的操作"。
线程安全队列累积事件，recorder 每轮 drain。

best-effort：macOS 需「输入监控/辅助功能」权限；没权限就降级为不报错、收不到事件。
跳过 mouse move（太吵），只收 click/scroll/key。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from loguru import logger

try:
    from pynput import keyboard, mouse

    _HAS_PYNPUT = True
except Exception:  # pragma: no cover
    _HAS_PYNPUT = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key_str(key) -> str:
    """把 pynput 的 key 对象转成可读字符串。"""
    try:
        if hasattr(key, "char") and key.char is not None:
            return key.char
        return str(key).replace("Key.", "")
    except Exception:
        return str(key)


class InputHook:
    """累积你的输入事件，供 recorder drain。"""

    def __init__(self) -> None:
        self._events: list[dict] = []
        self._lock = threading.Lock()
        self._mouse: "mouse.Listener | None" = None
        self._keyboard: "keyboard.Listener | None" = None

    def _push(self, event: dict) -> None:
        with self._lock:
            self._events.append(event)

    # ---- pynput 回调 ----
    def _on_click(self, x, y, button, pressed) -> None:
        if pressed:  # 只记按下，省一半
            self._push({"ts": _now(), "kind": "click", "x": int(x), "y": int(y),
                        "button": getattr(button, "name", str(button)), "key": None, "pressed": 1})

    def _on_scroll(self, x, y, dx, dy) -> None:
        self._push({"ts": _now(), "kind": "scroll", "x": int(x), "y": int(y),
                    "button": f"{dx},{dy}", "key": None, "pressed": None})

    def _on_press(self, key) -> None:
        self._push({"ts": _now(), "kind": "key_press", "x": None, "y": None,
                    "button": None, "key": _key_str(key), "pressed": 1})

    # ---- 生命周期 ----
    def start(self) -> bool:
        """启动监听。返回是否成功（pynput 不可用/无权限则 False）。"""
        if not _HAS_PYNPUT:
            logger.warning("pynput 不可用，输入事件捕获关闭。")
            return False
        try:
            self._mouse = mouse.Listener(on_click=self._on_click, on_scroll=self._on_scroll)
            self._keyboard = keyboard.Listener(on_press=self._on_press)
            self._mouse.start()
            self._keyboard.start()
            return True
        except Exception as e:  # pragma: no cover
            logger.warning("启动输入监听失败（多半缺『输入监控』权限）：{}", e)
            return False

    def drain(self) -> list[dict]:
        """取出并清空累积的事件。"""
        with self._lock:
            out = self._events
            self._events = []
        return out

    def stop(self) -> None:
        for li in (self._mouse, self._keyboard):
            if li is not None:
                try:
                    li.stop()
                except Exception:
                    pass
        self._mouse = self._keyboard = None
