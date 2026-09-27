"""Touch, keyboard and mouse injection through Win32 (InjectTouchInput / SendInput).

Coordinates are physical screen pixels (the process is per-monitor DPI aware), matching Windows-MCP screenshots.
Keyboard input is sent as hardware scan codes so games that read raw input or DirectInput see it too.
"""

from __future__ import annotations

import collections
import ctypes
import math
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)


def set_dpi_awareness() -> None:
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor aware v2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def screen_size() -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


# ---------------------------------------------------------------------------------------------------- touch

POINTER_FLAG_INRANGE = 0x00000002
POINTER_FLAG_INCONTACT = 0x00000004
POINTER_FLAG_DOWN = 0x00010000
POINTER_FLAG_UPDATE = 0x00020000
POINTER_FLAG_UP = 0x00040000
PT_TOUCH = 0x00000002
TOUCH_FEEDBACK_DEFAULT = 0x1
TOUCH_MASK_CONTACTAREA = 0x1
TOUCH_MASK_ORIENTATION = 0x2
TOUCH_MASK_PRESSURE = 0x4
MAX_CONTACTS = 10
FRAME_MS = 12


class POINTER_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerType", ctypes.c_uint32),
        ("pointerId", ctypes.c_uint32),
        ("frameId", ctypes.c_uint32),
        ("pointerFlags", ctypes.c_uint32),
        ("sourceDevice", wintypes.HANDLE),
        ("hwndTarget", wintypes.HWND),
        ("ptPixelLocation", wintypes.POINT),
        ("ptHimetricLocation", wintypes.POINT),
        ("ptPixelLocationRaw", wintypes.POINT),
        ("ptHimetricLocationRaw", wintypes.POINT),
        ("dwTime", wintypes.DWORD),
        ("historyCount", ctypes.c_uint32),
        ("InputData", ctypes.c_int32),
        ("dwKeyStates", wintypes.DWORD),
        ("PerformanceCount", ctypes.c_uint64),
        ("ButtonChangeType", ctypes.c_int),
    ]


class POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo", POINTER_INFO),
        ("touchFlags", ctypes.c_uint32),
        ("touchMask", ctypes.c_uint32),
        ("rcContact", wintypes.RECT),
        ("rcContactRaw", wintypes.RECT),
        ("orientation", ctypes.c_uint32),
        ("pressure", ctypes.c_uint32),
    ]


user32.InitializeTouchInjection.argtypes = [ctypes.c_uint32, wintypes.DWORD]
user32.InitializeTouchInjection.restype = wintypes.BOOL
user32.InjectTouchInput.argtypes = [ctypes.c_uint32, ctypes.POINTER(POINTER_TOUCH_INFO)]
user32.InjectTouchInput.restype = wintypes.BOOL

_touch_lock = threading.Lock()
_touch_ready = False


def _check_point(x: float, y: float) -> None:
    w, h = screen_size()
    if not (0 <= round(x) < w and 0 <= round(y) < h):
        raise ValueError(f"point ({round(x)}, {round(y)}) is outside the {w}x{h} screen")


def _ensure_touch() -> None:
    global _touch_ready
    if not _touch_ready:
        if not user32.InitializeTouchInjection(MAX_CONTACTS, TOUCH_FEEDBACK_DEFAULT):
            raise ctypes.WinError(ctypes.get_last_error())
        _touch_ready = True


def _contact(pointer_id: int, x: int, y: int, flags: int) -> POINTER_TOUCH_INFO:
    c = POINTER_TOUCH_INFO()
    c.pointerInfo.pointerType = PT_TOUCH
    c.pointerInfo.pointerId = pointer_id
    c.pointerInfo.pointerFlags = flags
    c.pointerInfo.ptPixelLocation.x = x
    c.pointerInfo.ptPixelLocation.y = y
    c.touchMask = TOUCH_MASK_CONTACTAREA | TOUCH_MASK_ORIENTATION | TOUCH_MASK_PRESSURE
    c.rcContact.left, c.rcContact.right = x - 2, x + 2
    c.rcContact.top, c.rcContact.bottom = y - 2, y + 2
    c.orientation = 90
    c.pressure = 32000
    return c


def _sample(keyframes: list[tuple[float, float, float]], t_ms: float) -> tuple[float, float]:
    """Linear interpolation over (time_ms, x, y) keyframes."""
    if t_ms <= keyframes[0][0]:
        return keyframes[0][1], keyframes[0][2]
    for (t1, x1, y1), (t2, x2, y2) in zip(keyframes, keyframes[1:]):
        if t_ms <= t2:
            f = 0.0 if t2 == t1 else (t_ms - t1) / (t2 - t1)
            return x1 + (x2 - x1) * f, y1 + (y2 - y1) * f
    return keyframes[-1][1], keyframes[-1][2]


def touch_keyframes(fingers: list[list[tuple[float, float, float]]]) -> dict:
    """Core touch gesture. Each finger is a list of (time_ms, x, y) keyframes, starting at time 0.

    All fingers go down at time 0, follow their keyframes, and lift together at the last keyframe time.
    """
    if not fingers or len(fingers) > MAX_CONTACTS:
        raise ValueError(f"need 1-{MAX_CONTACTS} fingers")
    for kf in fingers:
        if not kf:
            raise ValueError("every finger needs at least one point")
        for _, x, y in kf:
            _check_point(x, y)
    total_ms = max(max(kf[-1][0] for kf in fingers), FRAME_MS)
    if total_ms > 30000:
        raise ValueError("gestures are limited to 30 seconds")
    frames = max(1, int(total_ms // FRAME_MS))
    n = len(fingers)
    arr = POINTER_TOUCH_INFO * n
    with _touch_lock:
        _ensure_touch()
        last: list[tuple[int, int]] = []
        start = time.perf_counter()
        for i in range(frames + 1):
            t = total_ms * i / frames
            flags = (POINTER_FLAG_DOWN if i == 0 else POINTER_FLAG_UPDATE) | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
            last = [(int(round(px)), int(round(py))) for px, py in (_sample(kf, t) for kf in fingers)]
            if not user32.InjectTouchInput(n, arr(*[_contact(k, x, y, flags) for k, (x, y) in enumerate(last)])):
                err = ctypes.get_last_error()
                if i > 0:
                    user32.InjectTouchInput(n, arr(*[_contact(k, x, y, POINTER_FLAG_UP) for k, (x, y) in enumerate(last)]))
                raise ctypes.WinError(err)
            if i < frames:
                delay = start + (i + 1) * total_ms / frames / 1000 - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
        if not user32.InjectTouchInput(n, arr(*[_contact(k, x, y, POINTER_FLAG_UP) for k, (x, y) in enumerate(last)])):
            raise ctypes.WinError(ctypes.get_last_error())
    return {"fingers": n, "duration_ms": int(total_ms), "lifted_at": last}


def touch_tap(x: float, y: float, count: int = 1, hold_ms: int = 60, interval_ms: int = 120) -> dict:
    count = max(1, min(int(count), 10))
    hold_ms = max(FRAME_MS, min(int(hold_ms), 30000))
    for i in range(count):
        touch_keyframes([[(0, x, y), (hold_ms, x, y)]])
        if i < count - 1:
            time.sleep(max(0, interval_ms) / 1000)
    return {"tapped": [round(x), round(y)], "count": count, "hold_ms": hold_ms}


def touch_swipe(x1: float, y1: float, x2: float, y2: float, duration_ms: int = 300, hold_start_ms: int = 0,
                hold_end_ms: int = 0) -> dict:
    duration_ms = max(FRAME_MS, int(duration_ms))
    t0 = max(0, int(hold_start_ms))
    kf = [(0, x1, y1), (t0, x1, y1), (t0 + duration_ms, x2, y2), (t0 + duration_ms + max(0, int(hold_end_ms)), x2, y2)]
    return touch_keyframes([kf])


def touch_pinch(x: float, y: float, start_spread: float, end_spread: float, duration_ms: int = 500,
                angle_deg: float = 0) -> dict:
    """Two-finger pinch centered on (x, y); spread = distance between fingers. end > start zooms in."""
    a = math.radians(angle_deg)
    ux, uy = math.cos(a) / 2, math.sin(a) / 2
    d = max(FRAME_MS, int(duration_ms))
    f1 = [(0, x - ux * start_spread, y - uy * start_spread), (d, x - ux * end_spread, y - uy * end_spread)]
    f2 = [(0, x + ux * start_spread, y + uy * start_spread), (d, x + ux * end_spread, y + uy * end_spread)]
    return touch_keyframes([f1, f2])


def touch_path_gesture(paths: list[list[list[float]]], duration_ms: int) -> dict:
    """Each finger follows its list of [x, y] points, evenly spaced in time over duration_ms."""
    d = max(FRAME_MS, int(duration_ms))
    fingers = []
    for pts in paths:
        if not pts:
            raise ValueError("every finger needs at least one [x, y] point")
        if len(pts) == 1:
            fingers.append([(0, pts[0][0], pts[0][1]), (d, pts[0][0], pts[0][1])])
        else:
            fingers.append([(d * i / (len(pts) - 1), p[0], p[1]) for i, p in enumerate(pts)])
    return touch_keyframes(fingers)


# ---------------------------------------------------------------------------------------------------- keyboard / mouse

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008
MOUSEEVENTF_MOVE = 0x0001
MAPVK_VK_TO_VSC_EX = 4
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = ctypes.c_uint
user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
user32.MapVirtualKeyW.restype = ctypes.c_uint

VK: dict[str, int] = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "shift": 0x10, "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "pageup": 0x21,
    "pagedown": 0x22, "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E, "del": 0x2E, "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
    "apps": 0x5D, "contextmenu": 0x5D, "numlock": 0x90, "scrolllock": 0x91, "lshift": 0xA0, "rshift": 0xA1,
    "lctrl": 0xA2, "rctrl": 0xA3, "lalt": 0xA4, "ralt": 0xA5, "volumemute": 0xAD, "volumedown": 0xAE,
    "volumeup": 0xAF, "nexttrack": 0xB0, "prevtrack": 0xB1, "stop": 0xB2, "playpause": 0xB3,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0, "[": 0xDB, "\\": 0xDC,
    "]": 0xDD, "'": 0xDE, "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
}
VK.update({chr(c): c for c in range(ord("0"), ord("9") + 1)})
VK.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
VK.update({f"numpad{i}": 0x60 + i for i in range(10)})
ALWAYS_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x6F, 0xA3, 0xA5}


def key_names() -> list[str]:
    return sorted(VK)


def _key_input(name: str, up: bool) -> INPUT:
    key = name.strip().lower()
    if key not in VK:
        raise ValueError(f"unknown key '{name}'. Known keys: {', '.join(key_names())}")
    vk = VK[key]
    inp = INPUT(type=INPUT_KEYBOARD)
    sc_ex = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC_EX)
    scan = sc_ex & 0xFF
    if scan:
        flags = KEYEVENTF_SCANCODE
        if (sc_ex >> 8) in (0xE0, 0xE1) or vk in ALWAYS_EXTENDED:
            flags |= KEYEVENTF_EXTENDEDKEY
        inp.u.ki = KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags | (KEYEVENTF_KEYUP if up else 0))
    else:
        # Media and volume keys have no scan code: send the virtual key instead.
        inp.u.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_EXTENDEDKEY | (KEYEVENTF_KEYUP if up else 0))
    return inp


def _send(inputs: list[INPUT]) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    if user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)) != len(inputs):
        raise ctypes.WinError(ctypes.get_last_error())


_keys_down: collections.Counter = collections.Counter()  # key -> holds in progress
_key_lock = threading.Lock()


def key_press(keys: list[str], hold_ms: int = 50, repeat: int = 1, interval_ms: int = 100) -> dict:
    """Press keys together (a chord like ["ctrl", "c"], or one key), hold, then release in reverse order."""
    if not keys:
        raise ValueError("no keys given")
    for k in keys:
        _key_input(k, False)
    repeat = max(1, min(int(repeat), 100))
    hold_ms = max(0, min(int(hold_ms), 60000))
    # The lock covers sending and bookkeeping only, never the hold: a 5 s walk mustn't block a quick tap meanwhile.
    # Keys are counted, so a tap of a key someone else is holding doesn't let go of it.
    for i in range(repeat):
        try:
            for k in keys:
                with _key_lock:
                    if _keys_down[k] == 0:
                        _send([_key_input(k, False)])
                    _keys_down[k] += 1
                time.sleep(0.01)
            time.sleep(hold_ms / 1000)
        finally:
            for k in reversed(keys):
                with _key_lock:
                    _keys_down[k] -= 1
                    if _keys_down[k] <= 0:
                        del _keys_down[k]
                        _send([_key_input(k, True)])
        if i < repeat - 1:
            time.sleep(max(0, interval_ms) / 1000)
    return {"keys": keys, "hold_ms": hold_ms, "repeat": repeat}


def release_all_keys() -> list[str]:
    with _key_lock:
        released = sorted(k for k, n in _keys_down.items() if n > 0)
        for k in released:
            try:
                _send([_key_input(k, True)])
            except Exception:
                pass
        _keys_down.clear()
    return released


def type_text(text: str, interval_ms: int = 5) -> dict:
    """Type Unicode text into the focused window."""
    text = text.replace("\r\n", "\n")
    for ch in text:
        if ch == "\n":
            _send([_key_input("enter", False), _key_input("enter", True)])
        else:
            units = ch.encode("utf-16-le")
            for i in range(0, len(units), 2):
                code = int.from_bytes(units[i:i + 2], "little")
                down = INPUT(type=INPUT_KEYBOARD)
                down.u.ki = KEYBDINPUT(wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE)
                up = INPUT(type=INPUT_KEYBOARD)
                up.u.ki = KEYBDINPUT(wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
                _send([down, up])
        if interval_ms:
            time.sleep(interval_ms / 1000)
    return {"typed_chars": len(text)}


def mouse_move_relative(dx: int, dy: int, duration_ms: int = 200) -> dict:
    """Relative mouse motion (what games read for camera/look), spread smoothly over duration_ms."""
    duration_ms = max(0, min(int(duration_ms), 30000))
    steps = max(1, duration_ms // 8)
    sent_x = sent_y = 0
    start = time.perf_counter()
    for i in range(1, steps + 1):
        tx, ty = round(dx * i / steps), round(dy * i / steps)
        if tx != sent_x or ty != sent_y:
            inp = INPUT(type=INPUT_MOUSE)
            inp.u.mi = MOUSEINPUT(dx=tx - sent_x, dy=ty - sent_y, mouseData=0, dwFlags=MOUSEEVENTF_MOVE)
            _send([inp])
            sent_x, sent_y = tx, ty
        delay = start + i * duration_ms / steps / 1000 - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
    return {"moved": [sent_x, sent_y], "duration_ms": duration_ms}


_BUTTONS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}


def mouse_hold(button: str = "left", hold_ms: int = 500) -> dict:
    """Hold a mouse button at the current cursor position (e.g. mine/attack in a game), then release."""
    if button not in _BUTTONS:
        raise ValueError("button must be left, right or middle")
    down_flag, up_flag = _BUTTONS[button]
    hold_ms = max(0, min(int(hold_ms), 60000))
    d = INPUT(type=INPUT_MOUSE)
    d.u.mi = MOUSEINPUT(dwFlags=down_flag)
    u = INPUT(type=INPUT_MOUSE)
    u.u.mi = MOUSEINPUT(dwFlags=up_flag)
    _send([d])
    try:
        time.sleep(hold_ms / 1000)
    finally:
        _send([u])
    return {"button": button, "hold_ms": hold_ms}


def mouse_click(x: float, y: float, button: str = "left", count: int = 1, interval_ms: int = 80) -> dict:
    """Move the cursor to (x, y) in screen pixels and click (count=2 double-clicks). Works in apps that ignore touch."""
    _check_point(x, y)
    if button not in _BUTTONS:
        raise ValueError("button must be left, right or middle")
    down_flag, up_flag = _BUTTONS[button]
    user32.SetCursorPos(round(x), round(y))
    time.sleep(0.02)
    count = max(1, min(int(count), 3))
    for i in range(count):
        d = INPUT(type=INPUT_MOUSE)
        d.u.mi = MOUSEINPUT(dwFlags=down_flag)
        u = INPUT(type=INPUT_MOUSE)
        u.u.mi = MOUSEINPUT(dwFlags=up_flag)
        _send([d])
        time.sleep(0.03)
        _send([u])
        if i < count - 1:
            time.sleep(interval_ms / 1000)
    return {"clicked": [round(x), round(y)], "button": button, "count": count}


# ---------------------------------------------------------------------------------------------------- touch keyboard

def touch_keyboard(action: str = "status") -> dict:
    """Show, hide, toggle or query the Windows on-screen touch keyboard."""
    import comtypes
    import comtypes.client  # noqa: F401
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

    class ITipInvocation(IUnknown):
        _iid_ = GUID("{37c994e7-432b-4834-a2f7-dce1f13b834b}")
        _methods_ = [COMMETHOD([], HRESULT, "Toggle", (["in"], wintypes.HWND, "wnd"))]

    class IFrameworkInputPane(IUnknown):
        _iid_ = GUID("{5752238B-24F0-495A-82F1-2FD593056796}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Advise", (["in"], ctypes.c_void_p, "pWindow"), (["in"], ctypes.c_void_p, "pHandler"),
                      (["out"], ctypes.POINTER(wintypes.DWORD), "pdwCookie")),
            COMMETHOD([], HRESULT, "AdviseWithHWND", (["in"], wintypes.HWND, "hwnd"), (["in"], ctypes.c_void_p, "pHandler"),
                      (["out"], ctypes.POINTER(wintypes.DWORD), "pdwCookie")),
            COMMETHOD([], HRESULT, "Unadvise", (["in"], wintypes.DWORD, "dwCookie")),
            COMMETHOD([], HRESULT, "Location", (["out"], ctypes.POINTER(wintypes.RECT), "prcInputPaneScreenLocation")),
        ]

    def visible() -> tuple[bool, list[int]]:
        pane = comtypes.CoCreateInstance(GUID("{D5120AA3-46BA-44C5-822D-CA8092C1FC72}"), interface=IFrameworkInputPane,
                                         clsctx=comtypes.CLSCTX_ALL)
        r = pane.Location()
        rect = [r.left, r.top, r.right, r.bottom]
        return (r.right - r.left) > 0 and (r.bottom - r.top) > 0, rect

    def toggle() -> None:
        try:
            tip = comtypes.CoCreateInstance(GUID("{4CE576FA-83DC-4F88-951C-9D0782B4E376}"), interface=ITipInvocation,
                                            clsctx=comtypes.CLSCTX_INPROC_HANDLER | comtypes.CLSCTX_LOCAL_SERVER)
        except OSError:
            import subprocess
            subprocess.Popen([r"C:\Program Files\Common Files\microsoft shared\ink\TabTip.exe"])
            time.sleep(1.0)
            tip = comtypes.CoCreateInstance(GUID("{4CE576FA-83DC-4F88-951C-9D0782B4E376}"), interface=ITipInvocation,
                                            clsctx=comtypes.CLSCTX_INPROC_HANDLER | comtypes.CLSCTX_LOCAL_SERVER)
        tip.Toggle(user32.GetDesktopWindow())

    action = action.lower()
    if action not in ("status", "show", "hide", "toggle"):
        raise ValueError("action must be status, show, hide or toggle")
    before, rect = visible()
    if action == "toggle" or (action == "show" and not before) or (action == "hide" and before):
        toggle()
        for _ in range(20):
            time.sleep(0.1)
            now, rect = visible()
            if now != before:
                break
    now, rect = visible()
    return {"visible": now, "was_visible": before, "rect": rect if now else None}
