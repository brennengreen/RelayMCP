"""Device status and controls: battery/power, brightness, display mode, Windows power mode, CPU/memory,
and the keep-awake lease honored by the RelayMCP agent."""

from __future__ import annotations

import ctypes
import json
import time
import uuid
from ctypes import wintypes
from datetime import datetime, timedelta

from .speech import _powershell

from .paths import LEASE_FILE, STATE_FILE, USER_DIR

KEEPAWAKE_LOG = USER_DIR / "keepawake.log"

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


# ---------------------------------------------------------------------------------------------------- power / battery

class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte), ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte), ("BatteryLifeTime", wintypes.DWORD),
                ("BatteryFullLifeTime", wintypes.DWORD)]


def power_status() -> dict:
    s = SYSTEM_POWER_STATUS()
    if not kernel32.GetSystemPowerStatus(ctypes.byref(s)):
        raise ctypes.WinError(ctypes.get_last_error())
    out = {
        "plugged_in": {0: False, 1: True}.get(s.ACLineStatus),
        "battery_percent": None if s.BatteryLifePercent == 255 else s.BatteryLifePercent,
        "charging": bool(s.BatteryFlag & 8),
        "battery_saver": bool(s.SystemStatusFlag & 1),
    }
    if s.BatteryLifeTime != 0xFFFFFFFF:
        out["minutes_remaining"] = s.BatteryLifeTime // 60
    return out


# Windows 11 "Power mode" overlays (Settings > System > Power & battery), which Armoury Crate's modes build on.
POWER_MODES = {
    "best_power_efficiency": "961cc777-2547-4f9d-8174-7d86181b8a7a",
    "balanced": "00000000-0000-0000-0000-000000000000",
    "best_performance": "ded574b5-45a0-4f42-8737-46345c09c238",
}

_powrprof = ctypes.WinDLL("powrprof")


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def from_str(cls, s: str) -> "GUID":
        u = uuid.UUID(s)
        g = cls()
        g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
        g.Data4[:] = list(u.bytes[8:])
        return g

    def __str__(self) -> str:
        return str(uuid.UUID(fields=(self.Data1, self.Data2, self.Data3, self.Data4[0], self.Data4[1],
                                     int.from_bytes(bytes(self.Data4[2:]), "big"))))


def power_mode() -> dict:
    g = GUID()
    rc = _powrprof.PowerGetEffectiveOverlayScheme(ctypes.byref(g))
    if rc != 0:
        rc2 = _powrprof.PowerGetActualOverlayScheme(ctypes.byref(g))
        if rc2 != 0:
            raise OSError(f"PowerGetEffectiveOverlayScheme failed ({rc})")
    current = str(g)
    name = next((k for k, v in POWER_MODES.items() if v == current), current)
    return {"power_mode": name, "overlay_guid": current, "available": list(POWER_MODES)}


def set_power_mode(mode: str) -> dict:
    if mode not in POWER_MODES:
        raise ValueError(f"mode must be one of {', '.join(POWER_MODES)}")
    before = power_mode()["power_mode"]
    rc = _powrprof.PowerSetActiveOverlayScheme(ctypes.byref(GUID.from_str(POWER_MODES[mode])))
    if rc != 0:
        raise OSError(f"PowerSetActiveOverlayScheme failed ({rc})")
    return {"before": before, "after": power_mode()["power_mode"]}


def system_load() -> dict:
    import psutil
    vm = psutil.virtual_memory()
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.5),
        "cpu_freq_mhz": round(psutil.cpu_freq().current) if psutil.cpu_freq() else None,
        "memory_used_percent": vm.percent,
        "memory_available_gb": round(vm.available / 2**30, 1),
        "top_cpu_processes": _top_processes(),
    }


def _top_processes(n: int = 5) -> list[dict]:
    import psutil
    procs = list(psutil.process_iter(["name"]))
    for p in procs:
        try:
            p.cpu_percent(None)
        except Exception:
            pass
    time.sleep(0.5)
    rows = []
    for p in procs:
        try:
            if p.pid in (0, 4):  # System Idle Process, System
                continue
            rows.append({"name": p.info["name"], "pid": p.pid, "cpu_percent": round(p.cpu_percent(None) / psutil.cpu_count(), 1)})
        except Exception:
            pass
    return sorted(rows, key=lambda r: r["cpu_percent"], reverse=True)[:n]


# ---------------------------------------------------------------------------------------------------- brightness

_ole32 = ctypes.WinDLL("ole32") if hasattr(ctypes, "WinDLL") else None
if _ole32 is not None:
    _ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _ole32.CoInitializeEx.restype = ctypes.c_long  # a plain HRESULT: an already-initialized thread isn't an error


def wmi(namespace: str = "root\\WMI"):
    """A WMI connection over COM (scripting API, late-bound): ~10-50 ms per query instead of ~1.5 s for starting
    PowerShell. Works on any thread; COM is initialized there if it isn't already (in whatever mode it has)."""
    import comtypes.client  # first: importing comtypes initializes COM on this thread itself (and raises on conflict)
    _ole32.CoInitializeEx(None, 0)  # other threads: S_OK, S_FALSE and RPC_E_CHANGED_MODE all leave COM usable
    locator = comtypes.client.CreateObject("WbemScripting.SWbemLocator", dynamic=True)
    return locator.ConnectServer(".", namespace)


def wmi_first(query: str, namespace: str = "root\\WMI"):
    """The first object a WQL query returns, or None (also when the class doesn't exist on this machine)."""
    svc = wmi(namespace)
    try:
        objs = svc.ExecQuery(query)
        return objs.ItemIndex(0) if objs.Count else None
    except Exception:
        return None


NO_BRIGHTNESS = {"brightness_percent": None, "note": "this display has no software brightness control"}


def brightness() -> dict:
    try:
        obj = wmi_first("SELECT CurrentBrightness FROM WmiMonitorBrightness")
    except Exception:  # COM/WMI itself unavailable: the slow but proven path
        out = _powershell("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction SilentlyContinue | "
                          "Select-Object -First 1).CurrentBrightness")
        return {"brightness_percent": int(out)} if out.strip().isdigit() else dict(NO_BRIGHTNESS)
    return {"brightness_percent": int(obj.CurrentBrightness)} if obj is not None else dict(NO_BRIGHTNESS)


def set_brightness(percent: int) -> dict:
    percent = max(0, min(int(percent), 100))
    before = brightness()["brightness_percent"]
    try:
        methods = wmi_first("SELECT * FROM WmiMonitorBrightnessMethods")
    except Exception:
        methods = False  # COM/WMI unavailable: use PowerShell below
    if methods is None:
        raise RuntimeError("this display has no software brightness control")
    try:
        if methods is False:
            raise RuntimeError
        methods.WmiSetBrightness(0, percent)  # Timeout, Brightness
    except Exception:
        _powershell(
            "Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods | Select-Object -First 1 | "
            f"Invoke-CimMethod -MethodName WmiSetBrightness -Arguments @{{ Timeout = [uint32]0; Brightness = [byte]{percent} }} | Out-Null"
        )
    time.sleep(0.3)
    return {"before": before, "after": brightness()["brightness_percent"]}


# ---------------------------------------------------------------------------------------------------- display mode

class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD), ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD), ("dmFields", wintypes.DWORD),
        ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG), ("dmDisplayOrientation", wintypes.DWORD),
        ("dmDisplayFixedOutput", wintypes.DWORD), ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
        ("dmFormName", wintypes.WCHAR * 32), ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD), ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD), ("dmReserved1", wintypes.DWORD),
        ("dmReserved2", wintypes.DWORD), ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD),
    ]


ENUM_CURRENT_SETTINGS = -1
DM_PELSWIDTH, DM_PELSHEIGHT, DM_DISPLAYFREQUENCY = 0x80000, 0x100000, 0x400000
DISP_CHANGE = {0: "ok", 1: "restart required", -1: "failed", -2: "bad mode", -3: "not updated", -4: "bad flags",
               -5: "bad param", -6: "bad dual view"}
user32.EnumDisplaySettingsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DEVMODEW)]
user32.ChangeDisplaySettingsExW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(DEVMODEW), wintypes.HWND, wintypes.DWORD, ctypes.c_void_p]
user32.ChangeDisplaySettingsExW.restype = ctypes.c_long


def _mode(index: int) -> DEVMODEW | None:
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    return dm if user32.EnumDisplaySettingsW(None, index & 0xFFFFFFFF, ctypes.byref(dm)) else None


def display_mode() -> dict:
    cur = _mode(ENUM_CURRENT_SETTINGS)
    modes = set()
    i = 0
    while (m := _mode(i)) is not None and i < 2000:
        if m.dmBitsPerPel == 32:
            modes.add((m.dmPelsWidth, m.dmPelsHeight, m.dmDisplayFrequency))
        i += 1
    return {
        "current": {"width": cur.dmPelsWidth, "height": cur.dmPelsHeight, "refresh_hz": cur.dmDisplayFrequency},
        "available": [{"width": w, "height": h, "refresh_hz": hz} for w, h, hz in sorted(modes, reverse=True)],
    }


def set_display_mode(width: int | None = None, height: int | None = None, refresh_hz: int | None = None) -> dict:
    """Temporary change (not saved; reverts at sign-out/restart or by calling this again)."""
    cur = _mode(ENUM_CURRENT_SETTINGS)
    before = {"width": cur.dmPelsWidth, "height": cur.dmPelsHeight, "refresh_hz": cur.dmDisplayFrequency}
    dm = _mode(ENUM_CURRENT_SETTINGS)
    dm.dmFields = 0
    if width and height:
        dm.dmPelsWidth, dm.dmPelsHeight = int(width), int(height)
        dm.dmFields |= DM_PELSWIDTH | DM_PELSHEIGHT
    if refresh_hz:
        dm.dmDisplayFrequency = int(refresh_hz)
        dm.dmFields |= DM_DISPLAYFREQUENCY
    if not dm.dmFields:
        raise ValueError("give width+height and/or refresh_hz")
    rc = user32.ChangeDisplaySettingsExW(None, ctypes.byref(dm), None, 0, None)
    if rc != 0:
        raise RuntimeError(f"display change failed: {DISP_CHANGE.get(rc, rc)}")
    return {"before": before, "after": display_mode()["current"]}


# ---------------------------------------------------------------------------------------------------- keep-awake lease

def keep_awake(minutes: float) -> dict:
    """Keep the handheld and its screen awake for the next N minutes (0 cancels). Honored only at home."""
    USER_DIR.mkdir(parents=True, exist_ok=True)
    minutes = max(0.0, min(float(minutes), 24 * 60))
    if minutes <= 0:
        LEASE_FILE.unlink(missing_ok=True)
    else:
        until = datetime.now() + timedelta(minutes=minutes)
        LEASE_FILE.write_text(until.isoformat(timespec="seconds"), encoding="ascii")
    return keep_awake_status()


def keep_awake_status() -> dict:
    lease = None
    if LEASE_FILE.exists():
        try:
            lease = datetime.fromisoformat(LEASE_FILE.read_text(encoding="ascii").strip())
        except ValueError:
            lease = None
    state = STATE_FILE.read_text(encoding="ascii").strip() if STATE_FILE.exists() else "unknown"
    recent = []
    if KEEPAWAKE_LOG.exists():
        recent = KEEPAWAKE_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-3:]
    return {
        "location": state,
        "lease_until": lease.isoformat(timespec="seconds") if lease and lease > datetime.now() else None,
        "note": "The agent also keeps the handheld awake for 10 minutes after any MCP tool call or SSH command.",
        "recent_helper_log": recent,
    }


def overview_status() -> dict:
    w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    return {"screen": {"width": w, "height": h, "touch_points": user32.GetSystemMetrics(95)}, "power": power_status()}


def to_json(obj) -> str:
    return json.dumps(obj, indent=1, default=str)
