"""Turn-based play: a real-time game stays paused whenever the agent isn't acting.

A model thinks for seconds between calls, and a game doesn't wait: playing Minecraft, night fell and a zombie killed
the player during a few pauses for thought. With a pause profile ({"button": "start", "text": "Game is paused"}),
every input call (act, gamepad and touch tools, behaviors) resumes the game first and pauses it again when nothing of
the agent's is running, and screenshots taken while it's paused show the frame from just before the pause: the frozen
world, not the pause menu.

The pause text is what makes this safe: it's checked before resuming (the game, or the user, may have paused or
resumed it meanwhile; Minecraft pauses itself when it loses focus) and after pausing (a menu that doesn't pause the game
may have eaten the press). Without one, the pause state is only what these presses assume.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

WAIT_S = 1.5      # longest wait for the pause text to show up or go away
SETTLE_S = 0.3    # without a pause text: how long a pause menu takes to open or close
RECHECK_S = 0.12  # the text must stay gone this long: a fading menu's text stops being readable before it's gone


def _buttons(value) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


class Turns:
    """press(buttons) sends a gamepad press; grab() returns the current frame; sees(text, region) -> is it on screen
    (region None = anywhere); in_use() -> who is using a real controller (then the game isn't paused under their
    hands), or None.

    Profile: button (pauses), resume (default: button), text (shows while paused), region (where that text shows:
    faster checks), close (closes a menu in which the pause button does nothing, e.g. "b" for Minecraft's crafting
    screen; without it a second pause press is tried), settle_ms (let the last input play out before pausing)."""

    def __init__(self, press: Callable[[list[str]], Any], grab: Callable[[], Any], sees: Callable[..., bool],
                 in_use: Callable[[], Any] = lambda: None, settle: Callable[[], Any] = lambda: None):
        self._press, self._grab, self._sees, self._in_use = press, grab, sees, in_use
        self._settle = settle  # blocks until the screen stops changing (a menu finished fading out), or a timeout
        self._lock = threading.RLock()
        self.profile: dict | None = None
        self.paused = False
        self.frame = None
        self.paused_at: float | None = None
        self.busy = 0
        self.note: str | None = None
        self.switching = False  # pausing or resuming right now: menus fade in and out

    def configure(self, profile: dict | None) -> dict | None:
        with self._lock:
            if profile:
                button = profile.get("button")
                if not button:
                    raise ValueError('pause needs "button": the game\'s pause button, e.g. "start"')
                region = profile.get("region")
                if region is not None and (len(region) != 4 or region[2] <= region[0] or region[3] <= region[1]):
                    raise ValueError("pause region must be [left, top, right, bottom]")
                self.profile = {"button": button, "resume": profile.get("resume") or button,
                                "text": str(profile.get("text") or ""), "region": region,
                                "close": profile.get("close") or None,
                                "settle_ms": max(0, min(int(profile.get("settle_ms", 300)), 3000))}
                self.paused = bool(self.profile["text"]) and self._shows()
                self.frame, self.note = None, None
            else:
                self.profile, self.paused, self.frame, self.note = None, False, None, None
            return self.status()

    def _shows(self) -> bool:
        return bool(self._sees(self.profile["text"], self.profile.get("region")))

    def _gone_twice(self) -> bool:
        if self._shows():
            return False
        time.sleep(RECHECK_S)
        return not self._shows()

    def status(self) -> dict | None:
        if not self.profile:
            return None
        out: dict[str, Any] = {"pause": {k: v for k, v in self.profile.items() if v is not None}, "paused": self.paused}
        if self.note:
            out["note"] = self.note
        return out

    def running(self) -> bool:
        """Is the world on screen and moving (not paused, not a pause menu fading in or out)? Always, without turns."""
        return not self.profile or (not self.paused and not self.switching)

    def frozen(self):
        """The frame from just before the game was paused (the world as it is), or None while it runs."""
        return self.frame if self.profile and self.paused else None

    def begin(self) -> None:
        """An input call starts: make sure the game runs."""
        with self._lock:
            self.busy += 1
            if self.profile and self.busy == 1:
                self._resume()

    def end(self) -> None:
        """An input call finished: pause the game unless something else of the agent's is still running."""
        with self._lock:
            self.busy = max(0, self.busy - 1)
            if self.profile and not self.busy:
                self._pause()

    def _wait(self, cond: Callable[[], bool]) -> bool:
        end = time.monotonic() + WAIT_S
        while True:
            if cond():
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.05)

    def _resume(self) -> None:
        self.switching = True
        try:
            self._resume_now()
        finally:
            self.switching = False

    def _resume_now(self) -> None:
        text = self.profile["text"]
        paused = self._shows() if text else self.paused
        if not paused:
            self.paused, self.note = False, None
            return
        self._press(_buttons(self.profile["resume"]))
        if text:
            if not self._wait(self._gone_twice):
                self.note = f"pressed {self.profile['resume']} but {text!r} still shows: the game may still be paused"
                return
            self._settle()  # the menu fades out: input sent before it's gone lands in the menu
        else:
            time.sleep(SETTLE_S)
        self.paused, self.frame, self.note = False, None, None

    def _pause(self) -> None:
        self.switching = True
        try:
            self._pause_now()
        finally:
            self.switching = False

    def _pause_now(self) -> None:
        if self.paused:
            return
        who = self._in_use()
        if who:
            self.note = f"left running: {who} is in use"
            return
        text, button, close = self.profile["text"], self.profile["button"], self.profile.get("close")
        time.sleep(self.profile["settle_ms"] / 1000)  # let the last input play out, so the frozen frame shows its result
        frame = self._grab()
        self._press(_buttons(button))
        if not text:
            time.sleep(SETTLE_S)
        elif not self._wait(self._shows):
            # A menu in which the pause button does nothing (or which it only closed): close it, then pause.
            if close:
                self._press(_buttons(close))
                time.sleep(SETTLE_S)
                frame = self._grab()
            self._press(_buttons(button))
            if not self._wait(self._shows):
                self.note = (f"pressed {button}" + (f", {close}," if close else "") + f" and {button} again but "
                             f"{text!r} didn't show: the game may still be running")
                return
            self.note = f"closed a menu to pause ({close})" if close else None
            self.frame, self.paused_at, self.paused = frame, time.monotonic(), True
            return
        self.frame, self.paused_at, self.paused, self.note = frame, time.monotonic(), True, None
