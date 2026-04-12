import array
import ctypes
import ctypes.util
import os
import struct
from contextlib import contextmanager

import numpy as np

PIXEL_DATA_PTR = ctypes.POINTER(ctypes.c_ulong)
Atom = ctypes.c_ulong


class XFixesCursorImage(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_short),
        ("y", ctypes.c_short),
        ("width", ctypes.c_ushort),
        ("height", ctypes.c_ushort),
        ("xhot", ctypes.c_ushort),
        ("yhot", ctypes.c_ushort),
        ("cursor_serial", ctypes.c_ulong),
        ("pixels", PIXEL_DATA_PTR),
        ("atom", Atom),
        ("name", ctypes.c_char_p),
    ]


class Display(ctypes.Structure):
    pass


class Xcursor:
    def __init__(self, display=None):
        self.display = None
        self._owns_display = False

        xfixes = ctypes.util.find_library("Xfixes")
        if not xfixes:
            raise Exception("No XFixes library found.")
        self.XFixeslib = ctypes.cdll.LoadLibrary(xfixes)

        x11 = ctypes.util.find_library("X11")
        if not x11:
            raise Exception("No X11 library found.")
        self.xlib = ctypes.cdll.LoadLibrary(x11)

        xfixes_get_cursor_image = self.XFixeslib.XFixesGetCursorImage
        xfixes_get_cursor_image.restype = ctypes.POINTER(XFixesCursorImage)
        xfixes_get_cursor_image.argtypes = [ctypes.POINTER(Display)]
        self.XFixesGetCursorImage = xfixes_get_cursor_image

        xfree = self.xlib.XFree
        xfree.restype = ctypes.c_int
        xfree.argtypes = [ctypes.c_void_p]
        self.XFree = xfree

        xopen_display = self.xlib.XOpenDisplay
        xopen_display.restype = ctypes.POINTER(Display)
        xopen_display.argtypes = [ctypes.c_char_p]
        self.XOpenDisplay = xopen_display

        xclose_display = self.xlib.XCloseDisplay
        xclose_display.restype = ctypes.c_int
        xclose_display.argtypes = [ctypes.POINTER(Display)]
        self.XCloseDisplay = xclose_display

        if display is None:
            try:
                display_name = os.environ["DISPLAY"].encode("utf-8")
            except KeyError as exc:
                raise Exception("$DISPLAY not set.") from exc

            self.display = self.XOpenDisplay(display_name)
            self._owns_display = True
        elif isinstance(display, str):
            self.display = self.XOpenDisplay(display.encode("utf-8"))
            self._owns_display = True
        elif isinstance(display, bytes):
            self.display = self.XOpenDisplay(display)
            self._owns_display = True
        else:
            self.display = display

        if not self.display:
            raise Exception("Cannot open X display.")

    def close(self):
        if self.display is not None and self._owns_display:
            self.XCloseDisplay(self.display)
        self.display = None
        self._owns_display = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def argbdata_to_pixdata(self, data, length):
        if data is None or length < 1:
            return None

        byte_array = array.array("b", b"\x00" * 4 * length)

        offset, index = 0, 0
        while index < length:
            argb = data[index] & 0xFFFFFFFF
            rgba = (argb << 8) | (argb >> 24)
            b1 = (rgba >> 24) & 0xFF
            b2 = (rgba >> 16) & 0xFF
            b3 = (rgba >> 8) & 0xFF
            b4 = rgba & 0xFF

            struct.pack_into("=BBBB", byte_array, offset, b1, b2, b3, b4)
            offset += 4
            index += 1

        return byte_array

    @contextmanager
    def cursor_image(self):
        cursor_data = self.XFixesGetCursorImage(self.display)
        if not cursor_data:
            raise Exception("Cannot read XFixesGetCursorImage()")

        try:
            yield cursor_data[0]
        finally:
            self.XFree(cursor_data)

    def getCursorImageArray(self):
        with self.cursor_image() as data:
            height, width = data.height, data.width

            byte_array = self.argbdata_to_pixdata(data.pixels, height * width)
            image_array = np.array(byte_array, dtype=np.uint8)
            image_array = image_array.reshape(height, width, 4)
            del byte_array

            return image_array

    def getCursorImageArrayFast(self):
        with self.cursor_image() as data:
            height, width = data.height, data.width

            byte_array = ctypes.cast(data.pixels, ctypes.POINTER(ctypes.c_ulong * height * width))[0]
            image_array = np.array(bytearray(byte_array))
            image_array = image_array.reshape(height, width, 8)[:, :, (0, 1, 2, 3)]
            del byte_array

            return image_array

    def saveImage(self, image_array, path):
        from PIL import Image

        image = Image.fromarray(image_array)
        image.save(path)


if __name__ == "__main__":
    cursor = Xcursor()
    image_array = cursor.getCursorImageArrayFast()
    cursor.saveImage(image_array, "cursor_image.png")
