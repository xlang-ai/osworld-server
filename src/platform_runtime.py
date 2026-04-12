import ctypes
import os
import platform
import subprocess
from contextlib import contextmanager
from typing import Any, Tuple

import Xlib
from Xlib import X, display

platform_name: str = platform.system()

Accessible = Any
BaseWrapper = Any
Desktop = Any
StateType = Any
STATE_SHOWING = Any
ATAction = Any
Component = Any
ATText = Any
ATValue = Any
pyatspi = None
win32ui = None
win32gui = None
AppKit = None
ApplicationServices = None
Foundation = None
Quartz = None
oa_atomacos = None
_pyautogui = None

if platform_name == "Linux":
    try:
        import pyatspi
        from pyatspi import Accessible, StateType, STATE_SHOWING
        from pyatspi import Action as ATAction
        from pyatspi import Component
        from pyatspi import Text as ATText
        from pyatspi import Value as ATValue
    except ImportError:
        pyatspi = None

elif platform_name == "Windows":
    try:
        from pywinauto import Desktop
        from pywinauto.base_wrapper import BaseWrapper
        import win32gui
        import win32ui
    except ImportError:
        Desktop = Any
        BaseWrapper = Any
        win32gui = None
        win32ui = None

elif platform_name == "Darwin":
    try:
        import AppKit
        import ApplicationServices
        import Foundation
        import Quartz
        import oa_atomacos
    except ImportError:
        AppKit = None
        ApplicationServices = None
        Foundation = None
        Quartz = None
        oa_atomacos = None

KEYBOARD_KEYS = []
HAS_PYATSPI = pyatspi is not None
HAS_PYWINAUTO = platform_name == "Windows" and win32gui is not None and Desktop is not Any
HAS_MACOS_A11Y = platform_name == "Darwin" and Quartz is not None and ApplicationServices is not None and AppKit is not None

TIMEOUT = 1800
DEFAULT_STREAM_FPS = 4.0
DEFAULT_STREAM_FORMAT = "jpeg"
DEFAULT_STREAM_QUALITY = 70


@contextmanager
def managed_x_display():
    conn = display.Display()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_pyautogui():
    global _pyautogui
    global KEYBOARD_KEYS

    if _pyautogui is None:
        import pyautogui

        pyautogui.PAUSE = 0
        pyautogui.DARWIN_CATCH_UP_TIME = 0
        _pyautogui = pyautogui
        KEYBOARD_KEYS = list(pyautogui.KEYBOARD_KEYS)

    return _pyautogui


def subprocess_creation_flags() -> int:
    if platform_name == "Windows":
        return subprocess.CREATE_NO_WINDOW
    return 0


def get_machine_architecture() -> str:
    architecture = platform.machine().lower()
    if architecture in ["amd32", "amd64", "x86", "x86_64", "x86-64", "x64", "i386", "i686"]:
        return "amd"
    if architecture in ["arm64", "aarch64", "aarch32"]:
        return "arm"
    return "unknown"


def get_capture_display_name() -> str:
    return os.environ.get("DISPLAY", ":0.0")


def get_linux_screen_size() -> Tuple[int, int]:
    with managed_x_display() as conn:
        return conn.screen().width_in_pixels, conn.screen().height_in_pixels


def get_screen_size() -> Tuple[int, int]:
    if platform_name == "Linux":
        return get_linux_screen_size()
    if platform_name == "Windows":
        user32 = ctypes.windll.user32
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    raise RuntimeError(f"Unsupported platform for screen size: {platform_name}")
