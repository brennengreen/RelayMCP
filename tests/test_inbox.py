import threading
import time

import pytest

from relaymcp.device import inbox


def test_post_read_and_cursor():
    box = inbox.Inbox()
    box.post("player", {"x": 1, "health": 20})
    box.post("player", {"x": 2, "health": 18})
    box.post("world", {"time": "night"})
    r = box.read()
    assert r["latest"] == {"player": {"x": 2, "health": 18}, "world": {"time": "night"}} and r["cursor"] == 3
    assert [e["n"] for e in box.read("player")["events"]] == [1, 2]
    assert box.read(since=2)["events"][0]["topic"] == "world"
    assert box.read(max_items=1)["more"] == 2
    with pytest.raises(ValueError):
        box.post("bad topic!", {})


def test_wait_matches_key_value_or_text_and_times_out():
    box = inbox.Inbox()

    def later():
        time.sleep(0.1)
        box.post("mob", {"type": "iron_golem", "angry": False})
        time.sleep(0.1)
        box.post("mob", {"type": "iron_golem", "angry": True})

    threading.Thread(target=later).start()
    out = box.wait("mob", match="angry=true", timeout=5)
    assert out["event"]["data"]["angry"] is True and out["event"]["n"] == 2
    assert box.wait("mob", since=0, match="golem", timeout=1)["event"]["n"] == 1
    t = time.monotonic()
    assert box.wait("mob", match="creeper", timeout=0.2)["timed_out"] and time.monotonic() - t < 2


def test_a_wait_from_an_empty_inbox_cursor_finds_an_event_that_came_in_between():
    box = inbox.Inbox()  # fresh, as after a restart: its cursor is 0
    since = inbox.run_tool(box, "read", "reply")["cursor"]
    box.post("reply", {"reply": "tp", "ok": True})  # the game answered before the wait started
    out = inbox.run_tool(box, "wait", "reply", since=since, match="reply=tp", timeout=0.2)
    assert since == 0 and out["event"]["data"]["reply"] == "tp"
    assert inbox.run_tool(box, "wait", "reply", match="reply=tp", timeout=0.2)["timed_out"]  # no cursor: new only
    assert [e["n"] for e in inbox.run_tool(box, "read", "reply")["events"]] == [1]  # read: from the start


def test_only_local_programs_may_post():
    assert inbox.allowed({"host": "127.0.0.1:8767", inbox.STATE_HEADER: "1"})
    assert not inbox.allowed({"host": "127.0.0.1:8767"})  # a web page can't add the header without a preflight
    assert not inbox.allowed({"host": "evil.example:8767", inbox.STATE_HEADER: "1"})  # DNS rebinding


def test_topics_and_tool_errors():
    box = inbox.Inbox()
    box.post("a", 1)
    assert inbox.run_tool(box, "topics")["topics"]["a"]["updates"] == 1
    with pytest.raises(ValueError):
        inbox.run_tool(box, "delete")
