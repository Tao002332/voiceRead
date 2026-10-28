"""全局热键（Windows RegisterHotKey）。

注册失败不影响主流程：所有入口同时都在托盘菜单里。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QObject

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
WM_HOTKEY = 0x0312

# 虚拟键码
VK = {
    "space": 0x20,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "A": 0x41, "B": 0x42, "C": 0x43, "D": 0x44, "E": 0x45, "F": 0x46,
    "G": 0x47, "H": 0x48, "I": 0x49, "J": 0x4A, "K": 0x4B, "L": 0x4C,
    "M": 0x4D, "N": 0x4E, "O": 0x4F, "P": 0x50, "Q": 0x51, "R": 0x52,
    "S": 0x53, "T": 0x54, "U": 0x55, "V": 0x56, "W": 0x57, "X": 0x58,
    "Y": 0x59, "Z": 0x5A,
    "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34, "5": 0x35,
    "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39, "0": 0x30,
}


class _HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, owner: "GlobalHotkeys") -> None:
        super().__init__()
        self.owner = owner

    def nativeEventFilter(self, event_type, message):  # noqa: N802
        try:
            if event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY:
                    callback = self.owner._callbacks.get(int(msg.wParam))
                    if callback is not None:
                        callback()
                        return True, 0
        except Exception:  # noqa: BLE001 - 热键解析失败绝不影响主流程
            pass
        return False, 0


class GlobalHotkeys(QObject):
    def __init__(self, hwnd: int = 0) -> None:
        super().__init__()
        self.hwnd = hwnd or 0
        self._callbacks: dict[int, object] = {}
        self._next_id = 0x4A00
        self._filter = _HotkeyFilter(self)
        self._installed = False
        self.registered: list[str] = []

    def install(self) -> None:
        if self._installed:
            return
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.installNativeEventFilter(self._filter)
            self._installed = True

    def register(self, label: str, modifiers: int, key: str, callback) -> bool:
        self.install()
        vk = VK.get(key) if isinstance(key, str) else int(key)
        if not vk:
            return False
        hid = self._next_id
        self._next_id += 1
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            ok = user32.RegisterHotKey(self.hwnd, hid, modifiers, vk)
        except Exception:  # noqa: BLE001
            return False
        if not ok:
            return False
        self._callbacks[hid] = callback
        self.registered.append(label)
        return True

    def unregister_all(self) -> None:
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            for hid in list(self._callbacks):
                user32.UnregisterHotKey(self.hwnd, hid)
        except Exception:  # noqa: BLE001
            pass
        self._callbacks.clear()
        self.registered.clear()
        if self._installed:
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self._filter)
            self._installed = False
