"""Small global appearance settings and Windows' own desktop acrylic material."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
from pathlib import Path
import sys

from PySide6.QtCore import QObject, Property, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlPropertyMap

from hdusign.accounts import read_json, write_json


DEFAULTS = {"enabled": True, "transparency": 55, "tint": 35, "highlight": 35}


class WindowsBackdrop:
    """Use the native compositor; no desktop capture, assets or extra DLLs."""
    @staticmethod
    def supported():
        return (sys.platform == "win32" and QGuiApplication.platformName() == "windows"
                and sys.getwindowsversion().build >= 22621)

    def apply(self, window, enabled, dark):
        if not self.supported():
            return False
        try:
            dwm = ctypes.WinDLL("dwmapi")
            dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                                 ctypes.c_void_p, wintypes.DWORD]
            dwm.DwmSetWindowAttribute.restype = ctypes.c_long
            handle = int(window.winId())
            def attribute(key, value):
                value = ctypes.c_int(value)
                return dwm.DwmSetWindowAttribute(handle, key, ctypes.byref(value), ctypes.sizeof(value)) >= 0
            attribute(20, int(dark))
            attribute(33, 2)  # Native rounded window corners.
            attribute(38, 1)  # Qt's swap chain needs the native accent path below.

            class Accent(ctypes.Structure):
                _fields_ = [("state", ctypes.c_int), ("flags", ctypes.c_int),
                            ("color", wintypes.DWORD), ("animation", ctypes.c_int)]

            class CompositionData(ctypes.Structure):
                _fields_ = [("attribute", ctypes.c_int), ("data", ctypes.c_void_p),
                            ("size", ctypes.c_size_t)]

            # Windows' native accent compositor, also used by Qt frameless
            # window projects. This API is not a stable public Windows contract;
            # guard failure and retain the opaque QML fallback.
            accent = Accent(4 if enabled else 0, 2,
                            0x30202020 if dark else 0x30F2F2F2, 0)
            data = CompositionData(19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p),
                                   ctypes.sizeof(accent))
            function = ctypes.WinDLL("user32").SetWindowCompositionAttribute
            function.argtypes = [wintypes.HWND, ctypes.POINTER(CompositionData)]
            function.restype = wintypes.BOOL
            return bool(function(handle, ctypes.byref(data)))
        except (OSError, AttributeError, ValueError):
            return False


class Appearance(QObject):
    changed = Signal()

    def __init__(self, root, *, backend=None, parent=None):
        super().__init__(parent)
        self.path = Path(root) / "appearance.json"
        self._settings = dict(DEFAULTS)
        self._saved = dict(DEFAULTS)
        self._window = None
        self._dark = True
        self._active = False
        self._feedback = ""
        self.backend = backend or WindowsBackdrop()
        try:
            saved = read_json(self.path)
            if not isinstance(saved, dict):
                raise ValueError()
            for key, default in DEFAULTS.items():
                self._settings[key] = self._normalize(key, saved.get(key, default))
            self._saved = dict(self._settings)
        except (OSError, ValueError, TypeError):
            self._feedback = "外观设置无法读取，已使用默认外观。"
        self._values = QQmlPropertyMap.create(self)
        for key, value in self._settings.items():
            self._values.insert(key, value)
        self._values.freeze()

    @staticmethod
    def _normalize(key, value):
        if key == "enabled":
            return value if isinstance(value, bool) else DEFAULTS[key]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            return DEFAULTS[key]
        return max(0, min(100, int(round(value))))

    @Property(QObject, constant=True)
    def values(self):
        return self._values

    @Property(bool, constant=True)
    def nativeSupported(self):
        return bool(self.backend.supported())

    @Property(bool, notify=changed)
    def active(self):
        return self._active

    @Property(bool, notify=changed)
    def dirty(self):
        return self._settings != self._saved

    @Property(str, notify=changed)
    def feedback(self):
        return self._feedback

    @Property(str, notify=changed)
    def status(self):
        if not self._settings["enabled"]:
            return "玻璃效果已关闭。"
        if self._active:
            return "桌面透视已启用；模糊由 Windows 控制，液态高光与玻璃浓度可调。"
        return "当前环境未能启用桌面透视，已使用普通外观。需 Windows 11 22H2 及以上，效果也受系统透明设置影响。"

    def attach(self, window):
        self._window = window
        self._apply()

    def _apply(self):
        self._active = bool(self._window and self._settings["enabled"]
                            and self.backend.apply(self._window, True, self._dark))
        if self._window and not self._settings["enabled"]:
            self.backend.apply(self._window, False, self._dark)
        self.changed.emit()

    @Slot(bool)
    def setDark(self, dark):
        if dark != self._dark:
            self._dark = dark
            self._apply()

    @Slot(str, "QVariant")
    def setValue(self, key, value):
        if key not in DEFAULTS:
            return
        value = self._normalize(key, value)
        if self._settings[key] == value:
            return
        self._settings[key] = value
        self._values.insert(key, value)
        self._feedback = ""
        if key == "enabled":
            self._apply()
        else:
            self.changed.emit()

    @Slot(result=bool)
    def save(self):
        try:
            write_json(self.path, self._settings)
        except OSError:
            self._feedback = "外观设置保存失败；当前预览保留，请检查目录权限后重试。"
            self.changed.emit()
            return False
        self._saved = dict(self._settings)
        self._feedback = "全局外观已保存，切换账号和重启后保持。"
        self.changed.emit()
        return True

    @Slot()
    def reset(self):
        for key, value in DEFAULTS.items():
            self.setValue(key, value)
