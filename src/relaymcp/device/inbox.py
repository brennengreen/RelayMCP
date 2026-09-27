"""A state inbox: programs on the handheld (a game server script, an add-on, a mod, a test harness) push small JSON
states and events to http://127.0.0.1:<hardware port>/state/<topic>; agents and behaviors read them with the `state`
tool instead of reading pixels. Structured state is exact and costs a few tokens; a screenshot is neither.

    POST /state/player  {"x": 12.5, "y": 64, "health": 18}     (header X-Relay-State: 1; JSON body up to 64 KB)

Local programs only: the custom header can't be sent cross-site without a CORS preflight (never approved here), and
the Host check stops DNS-rebinding pages.
"""

from __future__ import annotations

import collections
import json
import re
import threading
import time

STATE_HEADER = "X-Relay-State"
MAX_BODY = 64 * 1024
TOPIC = re.compile(r"[A-Za-z0-9_.-]{1,40}")
KEEP = 1000


class Inbox:
    def __init__(self):
        self.latest: dict[str, dict] = {}
        self.events: collections.deque = collections.deque(maxlen=KEEP)
        self.n = 0
        self.cond = threading.Condition()

    def post(self, topic: str, data) -> int:
        if not TOPIC.fullmatch(topic or ""):
            raise ValueError("topic: 1-40 letters, digits, '_', '-' or '.'")
        with self.cond:
            self.n += 1
            at = time.time()
            self.latest[topic] = {"data": data, "at": at, "n": self.n}
            self.events.append({"n": self.n, "topic": topic, "data": data, "at": at})
            self.cond.notify_all()
            return self.n

    def read(self, topic: str = "", since: int = 0, max_items: int = 20) -> dict:
        with self.cond:
            events = [e for e in self.events if e["n"] > since and (not topic or e["topic"] == topic)]
            latest = {t: v["data"] for t, v in self.latest.items() if not topic or t == topic}
            return {"latest": latest, "events": [_short(e) for e in events[-max(1, min(max_items, 200)):]],
                    "more": max(0, len(events) - max_items) or None, "cursor": self.n}

    def wait(self, topic: str = "", since: int | None = None, match: str = "", timeout: float = 10.0) -> dict:
        """The first event after `since` (default: now) on topic whose JSON contains `match` (plain text, or
        key=value)."""
        deadline = time.monotonic() + max(0.0, min(float(timeout), 300.0))
        with self.cond:
            start = self.n if since is None else int(since)
            while True:
                for e in self.events:
                    if e["n"] > start and (not topic or e["topic"] == topic) and matches(e["data"], match):
                        return {"event": _short(e), "cursor": self.n}
                left = deadline - time.monotonic()
                if left <= 0:
                    return {"event": None, "timed_out": True, "cursor": self.n}
                self.cond.wait(min(left, 1.0))

    def topics(self) -> dict:
        with self.cond:
            now = time.time()
            return {"topics": {t: {"updates": sum(1 for e in self.events if e["topic"] == t),
                                   "age_s": round(now - v["at"], 1)} for t, v in self.latest.items()},
                    "cursor": self.n}


def matches(data, match: str) -> bool:
    if not match:
        return True
    if "=" in match and isinstance(data, dict):
        key, _, want = match.partition("=")
        value = data.get(key.strip())
        return value is not None and str(value).strip().lower() == want.strip().lower()
    return match.lower() in json.dumps(data, ensure_ascii=False).lower()


def _short(e: dict) -> dict:
    return {"n": e["n"], "topic": e["topic"], "data": e["data"], "t": time.strftime("%H:%M:%S", time.localtime(e["at"]))}


def allowed(headers) -> bool:
    host = (headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    return headers.get(STATE_HEADER) == "1" and host in ("127.0.0.1", "localhost", "::1")


def run_tool(inbox: Inbox, action: str, topic: str = "", since: int = 0, match: str = "", timeout: float = 10.0,
             max_items: int = 20) -> dict:
    action = (action or "read").lower()
    if action == "read":
        return inbox.read(topic, since, max_items)
    if action == "wait":
        return inbox.wait(topic, since or None, match, timeout)
    if action == "topics":
        return inbox.topics()
    raise ValueError("action must be read, wait or topics")


INBOX = Inbox()
