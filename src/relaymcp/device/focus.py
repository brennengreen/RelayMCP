"""Foreground (focus) handling, so input reaches the game.

Games only read the controller, keyboard and touch while their window is in front. Windows guards the foreground
(the "foreground lock"): a background process usually can't take it, and a plain SetForegroundWindow fails silently.
This module:

- reports what really has focus, using cheap Win32 calls only (never UI Automation, so a busy game can't stall it);
- brings a window to the front with the techniques that work under the lock, and verifies the result;
- remembers an *input target* (e.g. the game). Input tools refocus it if something steals focus, and every input
  result says whether it probably landed.

The pure helpers (matching, warnings, summaries) have no Windows dependency, so they're tested on any OS.
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

# Windows that pop up and take focus by themselves. ExternalControllerHelper is Armoury Crate's "external controller
# connected" notice, which appears when the virtual gamepad plugs in. AsHotplugCtrl (ASUS Hotplug Controller) keeps an
# invisible 0x0 window that can take focus and hold it: synthetic input can't take focus back from it.
FOCUS_STEALERS = {"externalcontrollerhelper.exe", "ashotplugctrl.exe"}
SHELL_PROCESSES = {"explorer.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe", "searchhost.exe",
                   "searchapp.exe", "lockapp.exe", "textinputhost.exe", "shellhost.exe"}
DESKTOP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
UNASSIGNED_VK = 0xE8   # an unassigned virtual key: counts as user input for the foreground lock, does nothing
VK_MENU = 0x12

_lock = threading.RLock()
_target: dict | None = None
_names: dict[int, tuple[float, str]] = {}


# ---------------------------------------------------------------------------------------------------- pure helpers

def app_name(process: str | None) -> str:
    p = process or ""
    return p[:-4] if p.lower().endswith(".exe") else p


def match_score(query: str, title: str | None, process: str | None) -> int:
    """How well a window matches a query (0 = no match): exact process or title beats a prefix, which beats a
    substring. Case-insensitive; the .exe suffix is optional."""
    q = (query or "").strip().lower()
    if not q:
        return 0
    t, p = (title or "").lower(), (process or "").lower()
    stem = app_name(p)
    if q in (p, stem):
        return 100
    if q == t:
        return 90
    if stem.startswith(q) or t.startswith(q):
        return 60
    if q in stem or q in t:
        return 40
    return 0


def short(info: dict | None, target: dict | None = None) -> dict | None:
    """The compact foreground summary that input results carry (~20 tokens)."""
    if not info:
        return None
    out = {"app": app_name(info.get("app") or info.get("process")), "title": (info.get("title") or "")[:60]}
    if target is not None:
        out["target"] = info.get("hwnd") == target.get("hwnd")
    return out


def invisible(fg: dict | None) -> bool:
    """A window nobody can see (zero area, or hidden) holds focus: input sent now goes nowhere."""
    if not fg:
        return False
    rect = fg.get("rect") or [0, 0, 1, 1]
    return fg.get("visible") is False or rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0


def is_app_window(info: dict | None) -> bool:
    """A window a person would put input into: not the desktop, shell, a known focus stealer or an invisible window."""
    if not info or info.get("desktop") or info.get("minimized") or invisible(info):
        return False
    proc = (info.get("process") or "").lower()
    return proc not in FOCUS_STEALERS and proc not in SHELL_PROCESSES


def warning_for(fg: dict | None, target: dict | None) -> str | None:
    """Why input probably didn't land, or None."""
    if not fg or fg.get("desktop"):
        return "nothing has focus (the desktop is in front); input probably didn't reach an app"
    proc = (fg.get("process") or "").lower()
    if proc == "externalcontrollerhelper.exe":
        return "Armoury Crate's controller notice has focus, so input went there instead of the game"
    if proc in FOCUS_STEALERS or invisible(fg):
        return (f"{app_name(fg.get('app') or proc)} (an invisible window) has focus, so input went nowhere. Windows "
                "won't let synthetic input take focus back from it: a real finger tap on the game fixes it")
    if fg.get("hung"):
        return f"{app_name(fg.get('app') or proc)} isn't responding"
    if target and fg.get("hwnd") != target.get("hwnd"):
        name = target.get("title") or target.get("query") or "the input target"
        return f"'{name}' isn't in front ({app_name(fg.get('app') or proc)} is), so input probably didn't reach it"
    return None


# ---------------------------------------------------------------------------------------------------- Win32

class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


_api = None
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM) if hasattr(ctypes, "WINFUNCTYPE") else None


def _win():
    """user32/kernel32/dwmapi with full prototypes (private instances, so other modules' argtypes aren't affected)."""
    global _api
    if _api is None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        d = ctypes.WinDLL("dwmapi")
        H = wintypes.HWND
        for name, args, res in (
            ("GetForegroundWindow", [], H), ("IsWindow", [H], wintypes.BOOL), ("IsWindowVisible", [H], wintypes.BOOL),
            ("IsIconic", [H], wintypes.BOOL), ("IsHungAppWindow", [H], wintypes.BOOL),
            ("GetWindowTextW", [H, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            ("GetClassNameW", [H, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            ("GetWindowThreadProcessId", [H, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            ("GetWindowRect", [H, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            ("GetWindowLongW", [H, ctypes.c_int], ctypes.c_long),
            ("GetAncestor", [H, wintypes.UINT], H),
            ("MonitorFromWindow", [H, wintypes.DWORD], wintypes.HANDLE),
            ("GetMonitorInfoW", [wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)], wintypes.BOOL),
            ("SetForegroundWindow", [H], wintypes.BOOL), ("BringWindowToTop", [H], wintypes.BOOL),
            ("ShowWindow", [H, ctypes.c_int], wintypes.BOOL),
            ("SwitchToThisWindow", [H, wintypes.BOOL], None),
            ("AttachThreadInput", [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL),
            ("EnumWindows", [_WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL),
            ("EnumChildWindows", [H, _WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL),
            ("PeekMessageW", [ctypes.POINTER(wintypes.MSG), H, wintypes.UINT, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
        ):
            f = getattr(u, name)
            f.argtypes, f.restype = args, res
        k.OpenProcess.argtypes, k.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.GetCurrentThreadId.restype = wintypes.DWORD
        d.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        _api = (u, k, d)
    return _api


def process_name(pid: int) -> str:
    if not pid:
        return ""
    cached = _names.get(pid)
    if cached and time.monotonic() - cached[0] < 30:
        return cached[1]
    u, k, _ = _win()
    name = ""
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        try:
            buf = ctypes.create_unicode_buffer(520)
            size = wintypes.DWORD(len(buf))
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = buf.value.rsplit("\\", 1)[-1]
        finally:
            k.CloseHandle(h)
    if len(_names) > 256:
        _names.clear()
    _names[pid] = (time.monotonic(), name)
    return name


def _pid_of(hwnd: int) -> tuple[int, int]:
    u, _, _ = _win()
    pid = wintypes.DWORD()
    tid = u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value, tid


def _text(fn, hwnd: int, size: int = 256) -> str:
    buf = ctypes.create_unicode_buffer(size)
    fn(hwnd, buf, size)
    return buf.value


def _cloaked(hwnd: int) -> bool:
    _, _, d = _win()
    val = wintypes.DWORD()
    try:
        return d.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(val), ctypes.sizeof(val)) == 0 and val.value != 0
    except OSError:
        return False


def _uwp_app(hwnd: int) -> str | None:
    """UWP apps live inside ApplicationFrameHost; the real app owns the child CoreWindow."""
    u, _, _ = _win()
    found: list[int] = []

    def cb(child, _):
        if _text(u.GetClassNameW, child) == "Windows.UI.Core.CoreWindow":
            found.append(child)
            return False
        return True

    u.EnumChildWindows(hwnd, _WNDENUMPROC(cb), 0)
    return process_name(_pid_of(found[0])[0]) if found else None


def window_info(hwnd: int | None) -> dict | None:
    if not hwnd:
        return None
    u, _, _ = _win()
    if not u.IsWindow(hwnd):
        return None
    pid, tid = _pid_of(hwnd)
    proc = process_name(pid)
    cls = _text(u.GetClassNameW, hwnd)
    r = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    mi = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
    mon = u.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    full = bool(mon and u.GetMonitorInfoW(mon, ctypes.byref(mi))) and \
        (r.left <= mi.rcMonitor.left and r.top <= mi.rcMonitor.top and r.right >= mi.rcMonitor.right and
         r.bottom >= mi.rcMonitor.bottom)
    info = {"hwnd": int(hwnd), "title": _text(u.GetWindowTextW, hwnd, 512), "class": cls, "pid": pid, "tid": tid,
            "process": proc, "app": proc, "rect": [r.left, r.top, r.right, r.bottom],
            "minimized": bool(u.IsIconic(hwnd)), "hung": bool(u.IsHungAppWindow(hwnd)),
            "visible": bool(u.IsWindowVisible(hwnd)), "desktop": cls in DESKTOP_CLASSES}
    info["fullscreen"] = bool(full) and not info["desktop"]
    if proc.lower() == "applicationframehost.exe":
        info["app"] = _uwp_app(hwnd) or proc
    return info


def foreground() -> dict | None:
    try:
        u, _, _ = _win()
        return window_info(u.GetForegroundWindow())
    except Exception:
        return None


def foreground_hwnd() -> int | None:
    try:
        return int(_win()[0].GetForegroundWindow() or 0) or None
    except Exception:
        return None


def windows(limit: int = 40) -> list[dict]:
    """Visible top-level app windows, front to back."""
    u, _, _ = _win()
    out: list[int] = []

    def cb(hwnd, _):
        if u.IsWindowVisible(hwnd) and not _cloaked(hwnd) and u.GetWindowTextW(hwnd, ctypes.create_unicode_buffer(2), 2):
            ex = u.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            if not ex & 0x80:  # WS_EX_TOOLWINDOW
                out.append(hwnd)
        return len(out) < limit * 3

    u.EnumWindows(_WNDENUMPROC(cb), 0)
    infos = [i for i in (window_info(h) for h in out) if i and not i["desktop"]]
    return infos[:limit]


def find_window(query: str | int) -> dict | None:
    if isinstance(query, int) or (isinstance(query, str) and query.strip().isdigit()):
        return window_info(int(query))
    best, score = None, 0
    for w in windows(80):
        s = max(match_score(query, w["title"], w["process"]), match_score(query, w["title"], w["app"]))
        if s > score:  # front-most wins ties (windows() is in z-order)
            best, score = w, s
    return best


# ---------------------------------------------------------------------------------------------------- focusing

def _tap_key(vk: int) -> None:
    from . import win_input
    down = win_input.INPUT(type=win_input.INPUT_KEYBOARD)
    down.u.ki = win_input.KEYBDINPUT(wVk=vk)
    up = win_input.INPUT(type=win_input.INPUT_KEYBOARD)
    up.u.ki = win_input.KEYBDINPUT(wVk=vk, dwFlags=win_input.KEYEVENTF_KEYUP)
    win_input._send([down, up])


def _wait_front(hwnd: int, seconds: float) -> bool:
    u, _, _ = _win()
    end = time.monotonic() + seconds
    while True:
        fg = u.GetForegroundWindow()
        if fg and (int(fg) == hwnd or int(u.GetAncestor(fg, 3) or 0) == hwnd):  # GA_ROOTOWNER
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.02)


def focus_window(target: str | int, wait_s: float = 0.4) -> dict:
    """Bring a window to the front and verify it. target: window title or process name (or a window handle).
    Never reports success unless the window really is in front."""
    info = find_window(target)
    if not info:
        return {"ok": False, "error": f"no window matches {target!r}",
                "windows": [short(w) for w in windows(8)], "foreground": short(foreground())}
    u, k, _ = _win()
    hwnd = info["hwnd"]
    if info["minimized"]:
        u.ShowWindow(hwnd, 9)  # SW_RESTORE
    elif u.GetForegroundWindow() == hwnd:
        return {"ok": True, "method": "already in front", "foreground": short(info)}
    u.PeekMessageW(ctypes.byref(wintypes.MSG()), None, 0, 0, 0)  # this thread needs a message queue to attach input

    def attach():
        fg = u.GetForegroundWindow()
        me, fg_tid, tgt_tid = k.GetCurrentThreadId(), _pid_of(fg)[1] if fg else 0, info["tid"]
        attached = [t for t in {fg_tid, tgt_tid} if t and t != me and u.AttachThreadInput(me, t, True)]
        try:
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
        finally:
            for t in attached:
                u.AttachThreadInput(me, t, False)

    def alt_tap():
        _tap_key(VK_MENU)
        u.SetForegroundWindow(hwnd)

    attempts = (
        ("SetForegroundWindow", lambda: u.SetForegroundWindow(hwnd)),
        ("after synthetic input", lambda: (_tap_key(UNASSIGNED_VK), u.SetForegroundWindow(hwnd))),
        ("AttachThreadInput", attach),
        ("Alt tap", alt_tap),
        ("SwitchToThisWindow", lambda: u.SwitchToThisWindow(hwnd, True)),
    )
    for name, attempt in attempts:
        try:
            attempt()
        except Exception:
            continue
        if _wait_front(hwnd, wait_s):
            _failed.clear()  # it worked: a later steal gets refocused again right away
            return {"ok": True, "method": name, "foreground": short(foreground())}
    fg = foreground()
    who = app_name((fg or {}).get("app")) or "another window"
    if fg and ((fg.get("process") or "").lower() in FOCUS_STEALERS or invisible(fg)):
        hint = (f"{who} is an invisible window holding focus, and Windows won't let synthetic input take it back. A "
                f"real finger tap on '{info.get('title') or target}' fixes it; ask the user if nobody is at the handheld")
    else:
        hint = f"Windows kept {who} in front (foreground lock). A tap on the target window usually gets it in front."
    return {"ok": False, "method": "none worked", "foreground": short(fg), "warning": hint}


# ---------------------------------------------------------------------------------------------------- input target

def set_target(query: str | None) -> dict | None:
    """Remember the window input is meant for (None clears it). Returns the matched window."""
    global _target
    with _lock:
        if not query:
            _target = None
            return None
        info = find_window(query)
        _target = {"hwnd": info["hwnd"], "query": str(query), "title": info["title"], "app": info["app"]} if info \
            else {"hwnd": 0, "query": str(query), "title": "", "app": ""}
        return info


def target() -> dict | None:
    """The remembered input target, re-found by name if its window was replaced (e.g. the game restarted)."""
    global _target
    with _lock:
        t = _target
        if t is None:
            return None
        if t["hwnd"] and window_info(t["hwnd"]):
            return t
        info = find_window(t["query"])
        if info:
            _target = {**t, "hwnd": info["hwnd"], "title": info["title"], "app": info["app"]}
        return _target


class ForegroundTracker:
    """Watches the foreground window (4 times a second, cheap Win32 calls): keeps a short history of who had focus
    and when, and the last normal app window, so focus stolen by a pop-up or an invisible helper can be put back
    even when no input target was set."""

    def __init__(self, history: int = 24):
        from collections import deque
        self.changes = deque(maxlen=history)
        self.last_app: dict | None = None
        self._hwnd = None
        self._thread = None

    def note(self, info: dict | None, at: float) -> None:
        hwnd = (info or {}).get("hwnd")
        if hwnd == self._hwnd:
            return
        self._hwnd = hwnd
        entry = {"at": time.strftime("%H:%M:%S", time.localtime(at)), **(short(info) or {"app": None})}
        if info and invisible(info):
            entry["invisible"] = True
        self.changes.append(entry)
        if is_app_window(info):
            self.last_app = {"hwnd": info["hwnd"], "title": info.get("title"), "app": info.get("app")}

    def start(self) -> None:
        if self._thread is not None:
            return

        def loop():
            while True:
                try:
                    self.note(foreground(), time.time())
                except Exception:
                    pass
                time.sleep(0.25)

        self._thread = threading.Thread(target=loop, name="foreground", daemon=True)
        self._thread.start()

    def recent(self, n: int = 8) -> list[dict]:
        return list(self.changes)[-n:]


TRACKER = ForegroundTracker()


RETRY_AFTER_S = 30.0
_failed: dict = {}  # the last refocus that didn't work: {"from": foreground hwnd, "to": hwnd, "at": time}


def _refocus(to_hwnd: int, fg_hwnd: int | None) -> dict:
    """Refocus, unless the same move failed moments ago (each try costs ~2 s of input latency, and a window holding
    the foreground lock keeps winning until someone touches the screen)."""
    f = _failed
    if f and f.get("from") == fg_hwnd and f.get("to") == to_hwnd and time.monotonic() - f["at"] < RETRY_AFTER_S:
        return {"refocused": False}
    ok = bool(focus_window(to_hwnd).get("ok"))
    if ok:
        _failed.clear()
    else:
        _failed.update({"from": fg_hwnd, "to": to_hwnd, "at": time.monotonic()})
    return {"refocused": ok}


def before_input() -> dict:
    """Called before sending input: brings the remembered target back to the front if something took focus. With no
    target, a focus stealer (a pop-up or an invisible helper window) gives focus back to the last app window."""
    t = target()
    if t and t["hwnd"]:
        fg = foreground_hwnd()
        if fg == t["hwnd"]:
            return {}
        return _refocus(t["hwnd"], fg)
    fg = foreground()
    last = TRACKER.last_app
    stolen = fg and ((fg.get("process") or "").lower() in FOCUS_STEALERS or invisible(fg))  # not the shell or Start
    if stolen and last and last["hwnd"] != fg.get("hwnd") and window_info(last["hwnd"]):
        return _refocus(last["hwnd"], fg.get("hwnd"))
    return {}


def diagnose() -> dict:
    """Why input might not register, in one call: what has focus (and whether it can take input), the input target,
    the last app window, recent focus changes, and how long since real input."""
    fg = foreground()
    t = target()
    out = {"foreground": short(fg, t if t and t["hwnd"] else None), "input_target": t and {k: t[k] for k in ("query", "title", "app")},
           "last_app_window": TRACKER.last_app and {k: TRACKER.last_app[k] for k in ("title", "app")},
           "focus_changes": TRACKER.recent(), "warning": warning_for(fg, t if t and t["hwnd"] else None)}
    if fg:
        out["foreground"].update({k: True for k in ("hung", "minimized") if fg.get(k)})
        if invisible(fg):
            out["foreground"]["invisible"] = True
    idle = last_input_seconds()
    if idle is not None:
        out["seconds_since_input"] = idle
    return out


def last_input_seconds() -> float | None:
    """Seconds since the last keyboard, mouse, touch or pen input on this session (Windows counts injected input too)."""
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
        u, k, _ = _win()
        li = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        if not u.GetLastInputInfo(ctypes.byref(li)):
            return None
        return round(((k.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000, 1)
    except Exception:
        return None


def annotate(result, pre: dict | None = None) -> dict:
    """Add the compact `foreground` report (and a warning if input probably went nowhere) to an input result."""
    if not isinstance(result, dict):
        result = {"result": result}
    t = target()
    fg = foreground()
    result["foreground"] = short(fg, t if t and t["hwnd"] else None)
    if pre and "refocused" in pre:
        result["refocused"] = pre["refocused"]
    w = warning_for(fg, t if t and t["hwnd"] else None)
    if w:
        result["warning"] = f"{result['warning']}; {w}" if result.get("warning") else w
    return result


def restore(hwnd: int | None) -> dict | None:
    """Put a window back in front after something (Armoury Crate's notice) stole focus."""
    if not hwnd or foreground_hwnd() == hwnd or not window_info(hwnd):
        return None
    return focus_window(hwnd)


def game_in_front() -> bool:
    """A game (or the remembered target) is in front: fullscreen app windows count, the desktop and shell don't."""
    fg = foreground()
    if not fg or fg["desktop"] or (fg["process"] or "").lower() in SHELL_PROCESSES:
        return False
    t = target()
    return bool(fg["fullscreen"] or (t and t["hwnd"] == fg["hwnd"]))
