"""Screen capture for agents: fast, small, and never stale.

DXGI Desktop Duplication (dxcam) returns a frame in a few milliseconds and reports when nothing has changed since the
previous one, so an unchanged screen costs nothing to re-check. When duplication isn't available (the secure desktop,
some exclusive-fullscreen games, remote sessions), GDI BitBlt takes over for a while. Frames are shrunk by a whole
factor, so image coordinates map back to the screen with simple math, then JPEG-encoded: a 960x540 image costs a model
~700 tokens instead of ~1,900 for a full-resolution PNG. No UI Automation is involved, so a hung window can't stall it.

Use one Grabber from one thread (server.SCREEN).
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import logging
import math
import time
from dataclasses import dataclass, replace
from typing import Any

log = logging.getLogger("relaymcp.capture")

DXGI_RETRY_S = 5.0       # after DXGI fails, use GDI for this long before trying DXGI again
FIRST_FRAME_WAIT_S = 0.3  # a new duplication can take a frame or two to deliver its first image


@dataclass(frozen=True)
class Frame:
    data: Any        # BGRA pixels, top-down rows (bytes or a C-contiguous numpy array)
    width: int
    height: int
    left: int        # screen position of the top-left pixel
    top: int
    backend: str     # "dxgi" or "gdi"
    at: float        # time.monotonic() at which this image was the screen's content
    new: bool = True  # False: DXGI reported no change since the previous grab


def whole_factor(width: int, height: int, max_side: int) -> int:
    """The smallest whole divisor that fits the longest side within max_side."""
    return max(1, math.ceil(max(width, height) / max(1, max_side)))


def clamp_region(region, left: int, top: int, width: int, height: int) -> tuple[int, int, int, int] | None:
    """[left, top, right, bottom] in screen pixels, clipped to the frame; None if nothing is left."""
    if not region:
        return None
    x0, y0, x1, y1 = (int(v) for v in region)
    x0, y0 = max(x0, left), max(y0, top)
    x1, y1 = min(x1, left + width), min(y1, top + height)
    if x1 - x0 < 2 or y1 - y0 < 2:
        raise ValueError(f"region {list(region)} is outside the screen ({width}x{height})")
    return x0, y0, x1, y1


def mapping(factor: int, ox: int, oy: int) -> str:
    """How an agent turns image coordinates into screen coordinates, in the plainest words."""
    if factor == 1 and ox == oy == 0:
        return "same as screen"
    fx = f"x*{factor}" if factor != 1 else "x"
    fy = f"y*{factor}" if factor != 1 else "y"
    return f"screen = ({fx}{f'+{ox}' if ox else ''}, {fy}{f'+{oy}' if oy else ''})"


def encode(frame: Frame, region=None, max_side: int = 960, quality: int = 70) -> tuple[bytes, dict]:
    """JPEG of the frame (optionally a region of it), shrunk by a whole factor to fit max_side."""
    from PIL import Image

    img = Image.frombuffer("RGB", (frame.width, frame.height), frame.data, "raw", "BGRX", 0, 1)
    ox, oy = frame.left, frame.top
    box = clamp_region(region, frame.left, frame.top, frame.width, frame.height)
    if box:
        img = img.crop((box[0] - frame.left, box[1] - frame.top, box[2] - frame.left, box[3] - frame.top))
        ox, oy = box[0], box[1]
    factor = whole_factor(img.width, img.height, max_side)
    if factor > 1:
        img = img.reduce(factor)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=max(20, min(int(quality), 95)))
    return buf.getvalue(), {"image": [img.width, img.height], "to_screen": mapping(factor, ox, oy)}


class Grabber:
    def __init__(self) -> None:
        self._cam = None
        self._dxgi_failed_at: float | None = None
        self._last: Frame | None = None
        self._sent: dict[str, str] = {}   # request key -> digest of the last image returned for it

    # --- backends ---------------------------------------------------------------------------------------------------

    def _camera(self):
        if self._cam is None:
            import dxcam
            self._cam = dxcam.create(output_color="BGRA", processor_backend="numpy", max_buffer_len=2)
        return self._cam

    def release(self) -> None:
        cam, self._cam, self._last = self._cam, None, None
        if cam is not None:
            try:
                cam.release()
            except Exception:
                pass

    def _grab_dxgi(self) -> Frame:
        cam = self._camera()
        deadline = time.monotonic() + FIRST_FRAME_WAIT_S
        while True:
            arr = cam.grab(new_frame_only=True)
            now = time.monotonic()
            if arr is not None:
                if not arr.flags.c_contiguous:
                    import numpy as np
                    arr = np.ascontiguousarray(arr)
                h, w = arr.shape[:2]
                self._last = Frame(arr, w, h, 0, 0, "dxgi", now, True)
                return self._last
            if self._last is not None:  # nothing changed since the previous grab, so it's still what's on screen
                return replace(self._last, at=now, new=False)
            if now > deadline:
                raise RuntimeError("DXGI delivered no frame")
            time.sleep(0.016)

    def _grab_gdi(self) -> Frame:
        from ctypes import wintypes

        user32, gdi32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
        user32.GetDC.restype = wintypes.HDC
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                           wintypes.HANDLE, wintypes.DWORD]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC,
                                 ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteDC.argtypes = [wintypes.HDC]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)  # primary screen, physical pixels (DPI-aware)
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # top-down
        bits = ctypes.c_void_p()
        bmp = gdi32.CreateDIBSection(screen, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        try:
            old = gdi32.SelectObject(mem, bmp)
            if not gdi32.BitBlt(mem, 0, 0, w, h, screen, 0, 0, 0x00CC0020):  # SRCCOPY
                raise ctypes.WinError()
            data = ctypes.string_at(bits, w * h * 4)
            gdi32.SelectObject(mem, old)
        finally:
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, screen)
        return Frame(data, w, h, 0, 0, "gdi", time.monotonic(), True)

    def grab(self) -> Frame:
        now = time.monotonic()
        if self._dxgi_failed_at is None or now - self._dxgi_failed_at > DXGI_RETRY_S:
            try:
                frame = self._grab_dxgi()
                self._dxgi_failed_at = None
                return frame
            except Exception as e:
                if self._dxgi_failed_at is None:
                    log.info("DXGI capture unavailable (%s); using GDI", e)
                self._dxgi_failed_at = now
                self.release()
        return self._grab_gdi()

    # --- what the tool returns --------------------------------------------------------------------------------------

    def screenshot(self, region=None, max_side: int = 960, quality: int = 70, only_if_changed: bool = False) -> dict:
        """{"jpeg": bytes | None, "meta": {...}}; jpeg is None when only_if_changed and nothing changed."""
        t0 = time.monotonic()
        frame = self.grab()
        grabbed = time.monotonic()
        jpeg, meta = encode(frame, region, max_side, quality)
        key = f"{region}|{max_side}|{quality}"
        digest = hashlib.blake2b(jpeg, digest_size=12).hexdigest()
        unchanged = self._sent.get(key) == digest
        self._sent[key] = digest
        meta.update({"source": frame.backend, "ms": round((time.monotonic() - t0) * 1000),
                     "grab_ms": round((grabbed - t0) * 1000)})
        if only_if_changed and unchanged:
            return {"jpeg": None, "meta": {"unchanged": True, "note": "same as the last screenshot with these settings",
                                           **meta}}
        return {"jpeg": jpeg, "meta": meta}


def cursor_pos() -> list[int] | None:
    try:
        from ctypes import wintypes
        pt = wintypes.POINT()
        return [pt.x, pt.y] if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)) else None
    except Exception:
        return None
