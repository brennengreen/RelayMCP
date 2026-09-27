"""Gamepad: a virtual Xbox 360 controller (ViGEmBus via vgamepad) plus XInput reads/rumble of physical controllers.

The virtual controller plugs in on first use (or ahead of time with connect()). It unplugs itself after the configured
idle time (device.json "gamepad_idle_minutes", default 30; 0 = never), but never while a fullscreen game or the
remembered input target is in front, and never when it was connected with keep_plugged. Re-plugging mid-game makes
games drop or re-assign the controller, and Armoury Crate's notice steals focus.
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

DEFAULT_IDLE_MINUTES = 30
MAX_SEQUENCE_MS = 60000
NOTICE_WAIT_SECONDS = 4.0
NOTICE_TITLE_FRAGMENT = "GamepadCustomizeExtCtrlr"  # Armoury Crate's "External controller connected" notice
from . import focus  # noqa: E402
from .paths import DISMISS_TASK, device_settings  # noqa: E402
CREATE_NO_WINDOW = 0x08000000

_WIN = hasattr(ctypes, "WinDLL")  # the pure helpers below also import (and get tested) off Windows
_user32 = ctypes.WinDLL("user32", use_last_error=True) if _WIN else None
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM) if _WIN else None
if _WIN:
    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]


def _armoury_notice_windows() -> list[int]:
    found: list[int] = []

    def cb(hwnd, _):
        if _user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            _user32.GetWindowTextW(hwnd, buf, 256)
            if NOTICE_TITLE_FRAGMENT in buf.value:
                found.append(hwnd)
        return True

    _user32.EnumWindows(_WNDENUMPROC(cb), 0)
    return found


def idle_limit_s() -> float | None:
    """Seconds of inactivity before the virtual pad unplugs itself, or None for never."""
    try:
        minutes = float(device_settings().get("gamepad_idle_minutes", DEFAULT_IDLE_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_IDLE_MINUTES
    return None if minutes <= 0 else minutes * 60


def should_unplug(idle_s: float, limit_s: float | None, pinned: bool, game_in_front: bool) -> bool:
    return not pinned and limit_s is not None and idle_s >= limit_s - 1 and not game_in_front


def _armoury_crate_running() -> bool:
    try:
        import psutil
        return any((p.info["name"] or "").lower().startswith("armourycrate") for p in psutil.process_iter(["name"]))
    except Exception:
        return True


def dismiss_armoury_notice(wait_seconds: float = 0.0) -> str | None:
    """Close Armoury Crate's "External controller connected" notice (raised when the virtual pad plugs in) without
    choosing anything, so the built-in controller stays enabled. Waits up to wait_seconds for it to appear."""
    deadline = time.monotonic() + max(0.0, wait_seconds)
    start = time.monotonic()
    while True:
        if _armoury_notice_windows():
            appeared = time.monotonic() - start
            import subprocess
            subprocess.run(["schtasks.exe", "/Run", "/TN", DISMISS_TASK], capture_output=True, timeout=8,
                           creationflags=CREATE_NO_WINDOW)
            for _ in range(30):
                time.sleep(0.1)
                if not _armoury_notice_windows():
                    return f"dismissed Armoury Crate's external-controller notice (appeared after {appeared:.1f}s)"
            return "WARNING: Armoury Crate's external-controller notice is still open; do not press A until it's closed"
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)

# XUSB button bits (same as XInput)
BUTTON_BITS = {
    "dpad_up": 0x0001, "dpad_down": 0x0002, "dpad_left": 0x0004, "dpad_right": 0x0008,
    "start": 0x0010, "back": 0x0020, "left_thumb": 0x0040, "right_thumb": 0x0080,
    "left_shoulder": 0x0100, "right_shoulder": 0x0200, "guide": 0x0400,
    "a": 0x1000, "b": 0x2000, "x": 0x4000, "y": 0x8000,
}
ALIASES = {
    "up": "dpad_up", "down": "dpad_down", "left": "dpad_left", "right": "dpad_right",
    "menu": "start", "view": "back", "select": "back", "share": "back",
    "ls": "left_thumb", "l3": "left_thumb", "rs": "right_thumb", "r3": "right_thumb",
    "lb": "left_shoulder", "l1": "left_shoulder", "rb": "right_shoulder", "r1": "right_shoulder",
    "home": "guide", "xbox": "guide",
}
TRIGGERS = {"lt": "left", "l2": "left", "left_trigger": "left", "rt": "right", "r2": "right", "right_trigger": "right"}


def button_names() -> list[str]:
    return sorted(set(BUTTON_BITS) | set(ALIASES) | set(TRIGGERS))


def _normalize(buttons: list[str] | None) -> tuple[int, float, float]:
    """Returns (button bitmask, left trigger, right trigger) for a list of names."""
    mask, lt, rt = 0, 0.0, 0.0
    for raw in buttons or []:
        name = raw.strip().lower().replace(" ", "_").replace("-", "_")
        if name in TRIGGERS:
            if TRIGGERS[name] == "left":
                lt = 1.0
            else:
                rt = 1.0
            continue
        name = ALIASES.get(name, name)
        if name not in BUTTON_BITS:
            raise ValueError(f"unknown button '{raw}'. Known: {', '.join(button_names())}")
        mask |= BUTTON_BITS[name]
    return mask, lt, rt


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(v)))


# ---------------------------------------------------------------------------------------------------- virtual pad

class VirtualPad:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pad = None
        self._vg = None
        self._last_used = 0.0
        self._timer: threading.Timer | None = None
        self._watcher: threading.Thread | None = None
        self.last_rumble: dict | None = None
        self.last_notice: str | None = None
        self.last_focus_restore: dict | None = None
        self.pinned = False
        self.plugged_at: float | None = None

    def _ensure(self):
        if self._pad is None:
            try:
                import vgamepad as vg
            except Exception as e:  # driver missing or DLL load failure
                raise RuntimeError(f"Virtual gamepad unavailable (is the ViGEmBus driver installed?): {e}") from e
            before = focus.foreground_hwnd()  # the game, usually; put back in front after Armoury Crate's notice
            self._vg = vg
            try:
                self._pad = vg.VX360Gamepad()
            except Exception as e:  # e.g. "The virtual device could not connect to ViGEmBus."
                raise RuntimeError(f"couldn't plug in the virtual controller ({e}). The ViGEmBus driver isn't running; "
                                   "tap 'Repair RelayMCP' on the handheld or restart it.") from e
            self.plugged_at = time.monotonic()
            try:
                self._pad.register_notification(callback_function=self._on_notification)
            except Exception:
                pass
            self._wait_ready(1.0)
            # Armoury Crate asks whether to disable the built-in controller (default: yes) when a controller plugs in,
            # and its notice takes focus. Close it (without choosing) before sending any input, give focus back, and
            # keep watching while the pad is plugged in.
            self.last_notice = dismiss_armoury_notice(NOTICE_WAIT_SECONDS if _armoury_crate_running() else 0.3)
            self.last_focus_restore = focus.restore(before)
            self._watcher = threading.Thread(target=self._watch_notice, args=(before,), daemon=True)
            self._watcher.start()
        self._touch()
        return self._pad

    def _wait_ready(self, seconds: float) -> bool:
        """Wait until XInput reports the new controller, so the first input isn't lost."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                slot = int(self._pad.get_index())
                if 0 <= slot < 4 and _xinput.XInputGetState(slot, ctypes.byref(XINPUT_STATE())) == 0:
                    return True
            except Exception:
                pass
            time.sleep(0.02)
        return False

    def _watch_notice(self, last_good: int | None) -> None:
        while self._pad is not None:
            try:
                fg = focus.foreground()
                if fg and (fg.get("process") or "").lower() not in focus.FOCUS_STEALERS and not fg.get("desktop"):
                    last_good = fg["hwnd"]
                result = dismiss_armoury_notice(0)
                if result:
                    self.last_notice = result
                    self.last_focus_restore = focus.restore(last_good)
            except Exception:
                pass
            time.sleep(0.25)

    def _on_notification(self, client, target, large_motor, small_motor, led_number, user_data):
        self.last_rumble = {"large_motor": int(large_motor), "small_motor": int(small_motor),
                            "led": int(led_number), "at": time.strftime("%H:%M:%S")}

    def _touch(self) -> None:
        self._last_used = time.monotonic()
        self._arm(idle_limit_s())

    def _arm(self, seconds: float | None) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if seconds is not None:
            self._timer = threading.Timer(max(1.0, seconds), self._idle_check)
            self._timer.daemon = True
            self._timer.start()

    def _idle_check(self) -> None:
        with self._lock:
            if self._pad is None:
                return
            limit = idle_limit_s()
            idle = time.monotonic() - self._last_used
            if should_unplug(idle, limit, self.pinned, focus.game_in_front()):
                self.disconnect()
            elif limit is not None:
                self._arm(max(60.0, limit - idle))  # a game is in front (or it's kept plugged): check again later

    def connect(self, keep_plugged: bool = True) -> dict:
        """Plug in ahead of time (e.g. right after launching a game) so the first real press lands."""
        with self._lock:
            newly = self._pad is None
            self._ensure()
            self.pinned = bool(keep_plugged)
        out = {"connected": True, "newly_plugged_in": newly, "xinput_slot": self.index(), "kept_plugged": self.pinned}
        if newly:
            out["ready_after_ms"] = round((time.monotonic() - (self.plugged_at or time.monotonic())) * 1000)
            if self.last_notice:
                out["armoury_crate"] = self.last_notice
            if self.last_focus_restore:
                out["focus_restored"] = self.last_focus_restore.get("ok")
        return out

    def disconnect(self) -> bool:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self.pinned = False
            if self._pad is None:
                return False
            try:
                self._pad.reset()
                self._pad.update()
            except Exception:
                pass
            pad, self._pad = self._pad, None
            del pad  # vgamepad unplugs the target in __del__
            import gc
            gc.collect()
            return True

    def index(self) -> int | None:
        pad = self._pad  # no lock: this is read while a sequence may be running
        if pad is None:
            return None
        try:
            return int(pad.get_index())
        except Exception:
            return None

    def connected(self) -> bool:
        return self._pad is not None

    def idle_seconds(self) -> float | None:
        return None if self._pad is None else round(time.monotonic() - self._last_used, 1)

    def _apply(self, mask: int, left: tuple[float, float], right: tuple[float, float], lt: float, rt: float) -> None:
        pad = self._pad
        vg = self._vg
        pad.reset()
        for bit in BUTTON_BITS.values():
            if mask & bit:
                pad.press_button(button=vg.XUSB_BUTTON(bit))
        pad.left_joystick_float(x_value_float=_clamp(left[0]), y_value_float=_clamp(left[1]))
        pad.right_joystick_float(x_value_float=_clamp(right[0]), y_value_float=_clamp(right[1]))
        pad.left_trigger_float(value_float=_clamp(lt, 0, 1))
        pad.right_trigger_float(value_float=_clamp(rt, 0, 1))
        pad.update()

    def stick(self, side: str, x: float, y: float) -> None:
        """Hold one stick where it is told (behaviors steer with this); the rest of the pad stays neutral."""
        with self._lock:
            self._ensure()
            left = (x, y) if side == "left_stick" else (0.0, 0.0)
            right = (x, y) if side == "right_stick" else (0.0, 0.0)
            self._apply(0, left, right, 0.0, 0.0)
            self._touch()

    def neutral(self) -> None:
        with self._lock:
            if self._pad is not None:
                self._apply(0, (0, 0), (0, 0), 0, 0)

    def _play(self, parsed: list[tuple]) -> list[float]:
        """Apply each step at its scheduled time (deadlines, 1 ms timer): steps don't drift, and each lands within
        about a millisecond. ramp_ms eases sticks and triggers from the previous step's values. Returns how late each
        step landed, in ms."""
        from .timing import HiResTimer, sleep_until
        late, prev = [], ((0.0, 0.0), (0.0, 0.0), 0.0, 0.0)
        with HiResTimer():
            t = time.perf_counter()
            for mask, ls, rs, lt, rt, ms, ramp in parsed:
                ramp = min(ramp, ms)
                late.append((time.perf_counter() - t) * 1000)
                if ramp:
                    frames = max(1, ramp // 8)
                    for i in range(1, frames + 1):
                        k = i / frames
                        self._apply(mask, (_mix(prev[0][0], ls[0], k), _mix(prev[0][1], ls[1], k)),
                                    (_mix(prev[1][0], rs[0], k), _mix(prev[1][1], rs[1], k)),
                                    _mix(prev[2], lt, k), _mix(prev[3], rt, k))
                        sleep_until(t + ramp * k / 1000)
                else:
                    self._apply(mask, ls, rs, lt, rt)
                t += ms / 1000
                sleep_until(t)
                prev = (ls, rs, lt, rt)
        return late

    def run_steps(self, steps: list[dict]) -> dict:
        """Each step: {buttons, left_stick [x,y], right_stick [x,y], left_trigger, right_trigger, ms}. State per step
        replaces the previous one; the pad returns to neutral at the end."""
        total = sum(max(0, int(s.get("ms", 100))) for s in steps)
        if total > MAX_SEQUENCE_MS:
            raise ValueError(f"sequence too long ({total} ms > {MAX_SEQUENCE_MS} ms)")
        parsed = []
        for s in steps:
            mask, lt, rt = _normalize(s.get("buttons"))
            ls = s.get("left_stick") or [0, 0]
            rs = s.get("right_stick") or [0, 0]
            parsed.append((mask, (float(ls[0]), float(ls[1])), (float(rs[0]), float(rs[1])),
                           max(lt, float(s.get("left_trigger", 0) or 0)), max(rt, float(s.get("right_trigger", 0) or 0)),
                           max(0, int(s.get("ms", 100))), max(0, int(s.get("ramp_ms", 0) or 0))))
        with self._lock:
            newly_plugged = self._pad is None
            self._ensure()
            try:
                late = self._play(parsed)
            finally:
                self._apply(0, (0, 0), (0, 0), 0, 0)
                self._touch()
        result = {"steps": len(parsed), "total_ms": total, "xinput_slot": self.index()}
        if len(late) >= 4:
            result["timing_ms_p95"] = round(sorted(late)[int(len(late) * 0.95)], 1)
        if newly_plugged:
            result["plugged_in"] = True
            if self.last_notice:
                result["armoury_crate"] = self.last_notice
            if self.last_focus_restore:
                result["focus_restored"] = self.last_focus_restore.get("ok")
        return result


def _mix(a: float, b: float, k: float) -> float:
    return a + (b - a) * k


PAD = VirtualPad()


def physical_active(deadzone: int = 9000, trigger: int = 40) -> str | None:
    """Is someone using a real controller (any slot but the virtual pad's)? Returns which, for "you took over"."""
    virtual = PAD.index()
    for i in range(4):
        if i == virtual:
            continue
        st = XINPUT_STATE()
        if _xinput.XInputGetState(i, ctypes.byref(st)) != 0:
            continue
        g = st.Gamepad
        if g.wButtons or g.bLeftTrigger > trigger or g.bRightTrigger > trigger or \
                max(abs(g.sThumbLX), abs(g.sThumbLY), abs(g.sThumbRX), abs(g.sThumbRY)) > deadzone:
            return f"controller in slot {i}"
    return None


# ---------------------------------------------------------------------------------------------------- XInput (physical)

class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", wintypes.WORD), ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", wintypes.DWORD), ("Gamepad", XINPUT_GAMEPAD)]


class XINPUT_VIBRATION(ctypes.Structure):
    _fields_ = [("wLeftMotorSpeed", wintypes.WORD), ("wRightMotorSpeed", wintypes.WORD)]


class XINPUT_BATTERY_INFORMATION(ctypes.Structure):
    _fields_ = [("BatteryType", ctypes.c_ubyte), ("BatteryLevel", ctypes.c_ubyte)]


_xinput = ctypes.WinDLL("xinput1_4") if _WIN else None
if _WIN:
    _xinput.XInputGetState.argtypes = [wintypes.DWORD, ctypes.POINTER(XINPUT_STATE)]
    _xinput.XInputGetState.restype = wintypes.DWORD
    _xinput.XInputSetState.argtypes = [wintypes.DWORD, ctypes.POINTER(XINPUT_VIBRATION)]
    _xinput.XInputSetState.restype = wintypes.DWORD
    _xinput.XInputGetBatteryInformation.argtypes = [wintypes.DWORD, ctypes.c_ubyte, ctypes.POINTER(XINPUT_BATTERY_INFORMATION)]
    _xinput.XInputGetBatteryInformation.restype = wintypes.DWORD

_BATTERY_TYPES = {0: "disconnected", 1: "wired", 2: "alkaline", 3: "nimh", 0xFF: "unknown"}
_BATTERY_LEVELS = {0: "empty", 1: "low", 2: "medium", 3: "full"}


def _decode(g: XINPUT_GAMEPAD) -> dict:
    return {
        "buttons": [n for n, bit in BUTTON_BITS.items() if g.wButtons & bit],
        "left_stick": [round(g.sThumbLX / 32767, 3), round(g.sThumbLY / 32767, 3)],
        "right_stick": [round(g.sThumbRX / 32767, 3), round(g.sThumbRY / 32767, 3)],
        "left_trigger": round(g.bLeftTrigger / 255, 3),
        "right_trigger": round(g.bRightTrigger / 255, 3),
    }


def xinput_states() -> list[dict]:
    out = []
    virtual_slot = PAD.index()
    for i in range(4):
        st = XINPUT_STATE()
        rc = _xinput.XInputGetState(i, ctypes.byref(st))
        if rc != 0:
            out.append({"slot": i, "connected": False})
            continue
        entry = {"slot": i, "connected": True, "virtual": i == virtual_slot, **_decode(st.Gamepad)}
        bat = XINPUT_BATTERY_INFORMATION()
        if _xinput.XInputGetBatteryInformation(i, 0, ctypes.byref(bat)) == 0:
            entry["battery"] = _BATTERY_TYPES.get(bat.BatteryType, str(bat.BatteryType))
            if bat.BatteryType not in (0, 1):
                entry["battery_level"] = _BATTERY_LEVELS.get(bat.BatteryLevel, str(bat.BatteryLevel))
        out.append(entry)
    return out


def xinput_watch(seconds: float, slot: int | None = None) -> dict:
    """Record every change on the physical/virtual controllers for a few seconds (what's being pressed)."""
    seconds = max(0.1, min(float(seconds), 30.0))
    events = []
    last: dict[int, int] = {}
    end = time.monotonic() + seconds
    start = time.monotonic()
    while time.monotonic() < end:
        for i in ([slot] if slot is not None else range(4)):
            st = XINPUT_STATE()
            if _xinput.XInputGetState(i, ctypes.byref(st)) == 0:
                if last.get(i) != st.dwPacketNumber:
                    if i in last:
                        events.append({"t_ms": int((time.monotonic() - start) * 1000), "slot": i, **_decode(st.Gamepad)})
                    last[i] = st.dwPacketNumber
        time.sleep(0.01)
        if len(events) > 400:
            break
    return {"seconds": seconds, "changes": len(events), "events": events[:400]}


def rumble(left: float = 0.5, right: float = 0.5, duration_ms: int = 300, slot: int = 0) -> dict:
    """Vibrate a physical controller (slot 0 is normally the Ally's built-in controller in gamepad mode)."""
    duration_ms = max(0, min(int(duration_ms), 5000))
    vib = XINPUT_VIBRATION(int(_clamp(left, 0, 1) * 65535), int(_clamp(right, 0, 1) * 65535))
    rc = _xinput.XInputSetState(int(slot), ctypes.byref(vib))
    if rc != 0:
        raise RuntimeError(f"no controller in XInput slot {slot} (error {rc}); is the Ally in gamepad mode?")
    try:
        time.sleep(duration_ms / 1000)
    finally:
        stop = XINPUT_VIBRATION(0, 0)
        _xinput.XInputSetState(int(slot), ctypes.byref(stop))
    return {"slot": slot, "left": left, "right": right, "duration_ms": duration_ms}
