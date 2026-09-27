"""Precise timing for input: Windows' default timer ticks every ~15.6 ms, so a plain time.sleep(0.02) can take 31 ms.
Gamepad sequences and behaviors schedule against deadlines with a 1 ms timer and a short spin instead."""

from __future__ import annotations

import time


class HiResTimer:
    """Ask Windows for a 1 ms timer while input is being timed (released afterwards)."""

    def __enter__(self):
        try:
            import ctypes
            self._winmm = ctypes.WinDLL("winmm")
            self._winmm.timeBeginPeriod(1)
        except Exception:
            self._winmm = None
        return self

    def __exit__(self, *exc):
        if self._winmm is not None:
            self._winmm.timeEndPeriod(1)


def sleep_until(deadline: float, clock=time.perf_counter) -> None:
    """Sleep most of the way, then spin the last ~1.5 ms: steps land within about a millisecond."""
    while True:
        left = deadline - clock()
        if left <= 0:
            return
        if left > 0.002:
            time.sleep(left - 0.0015)
