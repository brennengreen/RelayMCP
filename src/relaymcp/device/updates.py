"""Telling agents about updates they can't see. MCP clients load the tool list when a session starts, and this server
is stateless HTTP, so it can't push "the tools changed". Instead it remembers the tool set of its previous run; after
an update, a few results carry a short note naming the new tools and saying to restart the session (clients can't
be told apart, so the notes are spaced out and capped rather than repeated for everyone)."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

NOTICE_WINDOW_S = 45 * 60   # how long after an update results mention it
NOTICE_EVERY_S = 180        # at most one note per this many seconds (clients aren't told apart)
NOTICE_MAX_NEW = 3          # notes in all about new tools (sessions started before the update can't see them)
NOTICE_MAX_SAME = 1         # notes in all when only behavior changed: once is enough (it's context in every session)

# The tools of 0.1.0, the last release that didn't record its tool set: an upgrade from it still gets a notice.
V010_TOOLS = (
    "handheld_status gamepad_press gamepad_hold gamepad_sequence gamepad_status gamepad_watch gamepad_unplug "
    "controller_rumble touch_tap touch_long_press touch_swipe touch_pinch touch_gesture key_press type_text mouse_look "
    "mouse_hold touch_keyboard release_all_input audio_devices set_volume mic_record speaker_capture play_sound speak "
    "list_voices listen transcribe_audio_file set_brightness display_modes set_display_mode power_mode system_load "
    "keep_awake voice_assistant").split()


def tool_hash(names) -> str:
    return hashlib.sha256("\n".join(sorted(names)).encode()).hexdigest()[:10]


def record(names: list[str], version: str, path: Path, now: float | None = None, upgraded: bool = False) -> dict:
    """Compare this run's tools with the previous run's, remember this run's, and return what changed (if anything
    within the notice window). upgraded: the runtime ran before this feature existed (so compare with 0.1.0)."""
    now = time.time() if now is None else now
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prev = {"version": "0.1.0", "names": V010_TOOLS, "hash": tool_hash(V010_TOOLS)} if upgraded else None
    h = tool_hash(names)
    state = {"hash": h, "names": sorted(names), "version": version}
    if prev and (prev.get("hash") != h or prev.get("version") != version):
        state.update(changed_at=now, previous_version=prev.get("version"),
                     added=sorted(set(names) - set(prev.get("names") or [])),
                     removed=sorted(set(prev.get("names") or []) - set(names)))
    elif prev and prev.get("changed_at") and now - prev["changed_at"] < NOTICE_WINDOW_S:
        state.update({k: prev[k] for k in ("changed_at", "previous_version", "added", "removed", "shown") if k in prev})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass
    return state


def note_text(state: dict) -> str:
    at = time.strftime("%H:%M", time.localtime(state["changed_at"]))
    prev = state.get("previous_version")
    head = f"RelayMCP was updated at {at}" + (f" ({prev} -> {state['version']})" if prev and prev != state["version"] else "")
    added = state.get("added") or []
    if added:
        shown = ", ".join(added[:10]) + (f" and {len(added) - 10} more" if len(added) > 10 else "")
        return (f"{head}: new tools {shown}. Sessions started before {at} can't see them; start a new session (or "
                "reconnect this MCP server) to use them.")
    return f"{head}. Tools are unchanged; behavior may differ (see the RelayMCP changelog)."


class Notice:
    def __init__(self, state: dict | None = None, clock=time.time, path: Path | None = None):
        self.state, self._clock, self._last, self._path = state or {}, clock, 0.0, path
        self._lock = threading.Lock()

    def active(self) -> bool:
        at = self.state.get("changed_at")
        return bool(at) and self._clock() - at < NOTICE_WINDOW_S

    def take(self) -> str | None:
        """The note to attach to a result now, or None."""
        if not self.active():
            return None
        cap = NOTICE_MAX_NEW if self.state.get("added") else NOTICE_MAX_SAME
        with self._lock:
            now = self._clock()
            if now - self._last < NOTICE_EVERY_S or self.state.get("shown", 0) >= cap:
                return None
            self._last = now
            self.state["shown"] = self.state.get("shown", 0) + 1
            if self._path is not None:  # the count survives restarts, so a redeploy doesn't start the notes again
                try:
                    self._path.write_text(json.dumps(self.state), encoding="utf-8")
                except OSError:
                    pass
        return note_text(self.state)

    def summary(self) -> dict:
        out = {"count": len(self.state.get("names") or []), "hash": self.state.get("hash")}
        if self.active():
            out["updated_at"] = time.strftime("%H:%M", time.localtime(self.state["changed_at"]))
            out["new"] = self.state.get("added") or []
            if self.state.get("removed"):
                out["removed"] = self.state["removed"]
        return out


NOTICE = Notice()
