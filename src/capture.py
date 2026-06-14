import base64
import ctypes
import io
import logging
import os
import platform
import shutil
import subprocess
import tempfile
import time
from contextlib import suppress
from typing import Any, Dict, Optional, Tuple

from PIL import Image, ImageGrab

from .platform_runtime import DEFAULT_STREAM_FORMAT, DEFAULT_STREAM_QUALITY, get_pyautogui, win32gui, win32ui
from .pyxcursor import Xcursor

logger = logging.getLogger(__name__)


def _capture_linux_screenshot_with_subprocess(timeout_seconds: float = 8.0) -> Image.Image:
    """Capture Linux screenshots outside this long-lived server process.

    Xlib fatal errors can terminate the current process before Python exception
    handling runs. Keeping screenshot capture in a child process prevents one bad
    X request from taking down the OSWorld HTTP server.
    """
    tmp_file = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp_path = tmp_file.name
    tmp_file.close()
    with suppress(OSError):
        os.remove(tmp_path)

    commands = []
    if shutil.which("gnome-screenshot"):
        commands.append(["gnome-screenshot", "-p", "-f", tmp_path])
    if shutil.which("scrot"):
        commands.append(["scrot", "-p", "-z", tmp_path])
        commands.append(["scrot", "-z", tmp_path])
    if not commands:
        raise RuntimeError("No out-of-process screenshot tool found; install gnome-screenshot or scrot")

    errors = []
    try:
        for command in commands:
            with suppress(OSError):
                os.remove(tmp_path)
            try:
                subprocess.run(
                    command,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout_seconds,
                )
                if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) <= 0:
                    errors.append(f"{command[0]} produced no screenshot")
                    continue
                with Image.open(tmp_path) as image:
                    return image.copy()
            except Exception as exc:
                errors.append(f"{' '.join(command)}: {exc}")
        raise RuntimeError("Failed to capture screenshot out-of-process: " + " | ".join(errors))
    finally:
        with suppress(OSError):
            os.remove(tmp_path)


def _capture_linux_screenshot_with_pyautogui() -> Image.Image:
    pyautogui = get_pyautogui()
    screenshot = pyautogui.screenshot()
    try:
        with Xcursor() as cursor_obj:
            imgarray = cursor_obj.getCursorImageArrayFast()
        cursor_img = Image.fromarray(imgarray)
        cursor_x, cursor_y = pyautogui.position()
        screenshot.paste(cursor_img, (cursor_x, cursor_y), cursor_img)
    except Exception as exc:
        logger.warning(
            "Failed to capture cursor on Linux, screenshot will not have a cursor. Error: %s",
            exc,
        )
    return screenshot


def capture_screen_image() -> Image.Image:
    """Capture a screenshot with the cursor composited into the image."""
    user_platform = platform.system()

    if user_platform == "Windows":
        def get_cursor():
            hcursor = win32gui.GetCursorInfo()[1]
            screen_dc_handle = win32gui.GetDC(0)
            screen_dc = win32ui.CreateDCFromHandle(screen_dc_handle)
            hbmp = win32ui.CreateBitmap()
            hbmp.CreateCompatibleBitmap(screen_dc, 36, 36)
            mem_dc = screen_dc.CreateCompatibleDC()
            mem_dc.SelectObject(hbmp)
            mem_dc.DrawIcon((0, 0), hcursor)

            bmpinfo = hbmp.GetInfo()
            bmpstr = hbmp.GetBitmapBits(True)
            cursor = Image.frombuffer(
                "RGB",
                (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
                bmpstr,
                "raw",
                "BGRX",
                0,
                1,
            ).convert("RGBA")

            hotspot = win32gui.GetIconInfo(hcursor)[1:3]

            win32gui.DestroyIcon(hcursor)
            win32gui.DeleteObject(hbmp.GetHandle())
            mem_dc.DeleteDC()
            screen_dc.DeleteDC()
            win32gui.ReleaseDC(0, screen_dc_handle)

            pixdata = cursor.load()

            width, height = cursor.size
            for y in range(height):
                for x in range(width):
                    if pixdata[x, y] == (0, 0, 0, 255):
                        pixdata[x, y] = (0, 0, 0, 0)

            return cursor, hotspot

        ratio = ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100
        image = ImageGrab.grab(bbox=None, include_layered_windows=True)

        try:
            cursor, (hotspotx, hotspoty) = get_cursor()
            pos_win = win32gui.GetCursorPos()
            pos = (
                round(pos_win[0] * ratio - hotspotx),
                round(pos_win[1] * ratio - hotspoty),
            )
            image.paste(cursor, pos, cursor)
        except Exception as exc:
            logger.warning(
                "Failed to capture cursor on Windows, screenshot will not have a cursor. Error: %s",
                exc,
            )
        return image

    if user_platform == "Linux":
        if os.environ.get("OSWORLD_ALLOW_IN_PROCESS_X11_CAPTURE") == "1":
            return _capture_linux_screenshot_with_pyautogui()
        return _capture_linux_screenshot_with_subprocess()

    if user_platform == "Darwin":
        import tempfile

        tmp_file = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_file.close()
        try:
            subprocess.run(["screencapture", "-C", tmp_file.name], check=True)
            with Image.open(tmp_file.name) as image:
                return image.copy()
        finally:
            with suppress(OSError):
                os.remove(tmp_file.name)

    raise RuntimeError(f"The platform you're using ({user_platform}) is not currently supported")


def encode_image_bytes(
    image: Image.Image,
    image_format: str = "PNG",
    quality: int = DEFAULT_STREAM_QUALITY,
) -> Tuple[bytes, str]:
    normalized_format = image_format.upper()
    save_image = image
    save_kwargs: Dict[str, Any] = {}

    if normalized_format in {"JPEG", "JPG"}:
        normalized_format = "JPEG"
        if save_image.mode not in ("RGB", "L"):
            save_image = save_image.convert("RGB")
        save_kwargs["quality"] = max(1, min(int(quality), 95))
        save_kwargs["optimize"] = True
        mime_type = "image/jpeg"
    elif normalized_format == "PNG":
        save_kwargs["optimize"] = True
        mime_type = "image/png"
    else:
        raise ValueError(f"Unsupported image format: {image_format}")

    buffer = io.BytesIO()
    save_image.save(buffer, format=normalized_format, **save_kwargs)
    return buffer.getvalue(), mime_type


def coerce_stream_float(value: Optional[str], default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def coerce_stream_int(value: Optional[str], default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def normalize_stream_format(value: Optional[str]) -> str:
    normalized = (value or DEFAULT_STREAM_FORMAT).strip().lower()
    if normalized in {"jpg", "jpeg"}:
        return "jpeg"
    if normalized == "png":
        return "png"
    return DEFAULT_STREAM_FORMAT


def build_stream_frame_payload(
    session_id: str,
    frame_id: int,
    image_format: str,
    quality: int,
) -> Dict[str, Any]:
    image = capture_screen_image()
    image_bytes, mime_type = encode_image_bytes(image, image_format=image_format, quality=quality)
    return {
        "type": "frame",
        "session_id": session_id,
        "frame_id": frame_id,
        "timestamp": time.time(),
        "mime_type": mime_type,
        "width": image.width,
        "height": image.height,
        "encoding": "base64",
        "data": base64.b64encode(image_bytes).decode("ascii"),
    }
