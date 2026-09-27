"""A focus stealer on a real Windows desktop (the CI runner): an invisible 0x0 layered window, like ASUS'
AsHotplugCtrl, takes focus; input results must say so, and before_input() must give focus back to the last app window.
Run with RELAYMCP_DEVICE_TESTS=1."""

import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RELAYMCP_DEVICE_TESTS") != "1", reason="Windows device tests (CI)")

APP = ("import tkinter as t; r = t.Tk(); r.title('RelayStealTarget'); r.geometry('500x300+100+100'); "
       "r.after(60000, r.destroy); r.mainloop()")

STEALER = r"""
import ctypes, ctypes.wintypes as w, sys, time
u, k = ctypes.windll.user32, ctypes.windll.kernel32
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
u.DefWindowProcW.restype = ctypes.c_ssize_t
u.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
proc = WNDPROC(lambda h, m, wp, lp: u.DefWindowProcW(h, m, wp, lp))
class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", w.HINSTANCE), ("hIcon", w.HICON), ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]
k.GetModuleHandleW.restype = w.HMODULE
k.GetModuleHandleW.argtypes = [w.LPCWSTR]
hinst = k.GetModuleHandleW(None)
wc = WNDCLASSW(lpfnWndProc=proc, hInstance=hinst, lpszClassName="RELAYSTEALER")
u.RegisterClassW(ctypes.byref(wc))
u.CreateWindowExW.restype = w.HWND
u.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
hwnd = u.CreateWindowExW(0x00080080, "RELAYSTEALER", "RelayStealer", 0x94000000, 0, 0, 0, 0, None, None, hinst, None)
print(hwnd, flush=True)
msg = w.MSG()
end = time.time() + 60
while time.time() < end:
    while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
        u.TranslateMessage(ctypes.byref(msg)); u.DispatchMessageW(ctypes.byref(msg))
    time.sleep(0.02)
"""


@pytest.fixture
def scene():
    pytest.importorskip("tkinter")
    from relaymcp.device import focus
    app = subprocess.Popen([sys.executable, "-c", APP])
    stealer = subprocess.Popen([sys.executable, "-c", STEALER], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    first = stealer.stdout.readline().strip()
    assert first.isdigit(), f"the helper window didn't start: {first} {stealer.stdout.read()[-500:]}"
    hwnd = int(first)
    deadline = time.time() + 20
    while time.time() < deadline and not focus.find_window("RelayStealTarget"):
        time.sleep(0.2)
    if not focus.find_window("RelayStealTarget"):
        app.kill(), stealer.kill()
        pytest.skip("test window didn't appear (no interactive desktop?)")
    yield focus, hwnd
    app.kill()
    stealer.kill()


def test_invisible_stealer_is_flagged_and_focus_goes_back(scene):
    focus, stealer_hwnd = scene
    focus.set_target(None)
    assert focus.focus_window("RelayStealTarget")["ok"]
    focus.TRACKER.note(focus.foreground(), time.time())
    assert focus.TRACKER.last_app["title"] == "RelayStealTarget"

    took = focus.focus_window(stealer_hwnd)  # the "stealer" takes focus (our own strategies do it on the runner)
    if not took.get("ok"):
        pytest.skip(f"couldn't put the invisible window in front on this runner: {took}")
    fg = focus.foreground()
    assert focus.invisible(fg), fg
    assert "invisible window" in (focus.warning_for(fg, None) or "")
    focus.TRACKER.note(fg, time.time())
    assert focus.diagnose()["foreground"].get("invisible") is True

    pre = focus.before_input()  # no input target set: the last app window comes back
    print("\nbefore_input:", pre, "->", focus.short(focus.foreground()))
    assert pre.get("refocused") is True
    assert focus.foreground()["title"] == "RelayStealTarget"
    changes = [c["app"] for c in focus.TRACKER.recent()]
    assert changes[-1] and "python" in changes[-1].lower()
