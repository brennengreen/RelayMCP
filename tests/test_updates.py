import asyncio
import json

from relaymcp.device import lean, updates

OLD = ["gamepad_press", "handheld_status", "touch_tap"]
NEW = OLD + ["act", "observe", "screenshot"]


def test_first_run_is_a_silent_baseline(tmp_path):
    state = updates.record(OLD, "0.1.0", tmp_path / "seen.json", now=1000)
    assert "changed_at" not in state and not updates.Notice(state).active()
    assert json.loads((tmp_path / "seen.json").read_text())["names"] == sorted(OLD)


def test_update_names_the_new_tools_and_survives_restarts(tmp_path):
    path = tmp_path / "seen.json"
    updates.record(OLD, "0.1.0", path, now=1000)
    state = updates.record(NEW, "0.1.1", path, now=2000)
    assert state["added"] == ["act", "observe", "screenshot"] and state["previous_version"] == "0.1.0"
    again = updates.record(NEW, "0.1.1", path, now=2600)  # a restart without changes keeps the notice going
    assert again["changed_at"] == 2000 and again["added"] == state["added"]
    later = updates.record(NEW, "0.1.1", path, now=2000 + updates.NOTICE_WINDOW_S + 1)
    assert "changed_at" not in later


def test_notice_is_rate_limited_and_expires(tmp_path):
    path = tmp_path / "seen.json"
    updates.record(OLD, "0.1.0", path, now=1000)
    state = updates.record(NEW, "0.1.1", path, now=2000)
    clock = [2010.0]
    n = updates.Notice(state, clock=lambda: clock[0])
    first = n.take()
    assert "new tools act, observe, screenshot" in first and "0.1.0 -> 0.1.1" in first and "new session" in first
    assert n.take() is None                       # not in every result
    clock[0] += updates.NOTICE_EVERY_S + 1
    assert n.take()
    assert n.summary()["new"] == ["act", "observe", "screenshot"] and n.summary()["count"] == 6
    clock[0] = 2000 + updates.NOTICE_WINDOW_S + 1
    assert n.take() is None and "new" not in n.summary()


def test_notes_are_capped_and_the_count_survives_a_restart(tmp_path):
    path = tmp_path / "seen.json"
    updates.record(OLD, "0.1.0", path, now=1000)
    clock = [2010.0]
    n = updates.Notice(updates.record(NEW, "0.1.1", path, now=2000), clock=lambda: clock[0], path=path)
    shown = []
    for _ in range(10):
        shown.append(n.take())
        clock[0] += updates.NOTICE_EVERY_S + 1
    assert sum(1 for s in shown if s) == updates.NOTICE_MAX_NEW
    restarted = updates.Notice(updates.record(NEW, "0.1.1", path, now=clock[0]), clock=lambda: clock[0], path=path)
    assert restarted.active() and restarted.take() is None  # a redeploy doesn't start the notes over
    assert restarted.summary()["new"] == ["act", "observe", "screenshot"]  # handheld_status still says so


def test_a_behavior_only_update_is_mentioned_once(tmp_path):
    path = tmp_path / "seen.json"
    updates.record(OLD, "0.1.0", path, now=1000)
    clock = [2010.0]
    n = updates.Notice(updates.record(OLD, "0.1.1", path, now=2000), clock=lambda: clock[0], path=path)
    assert "Tools are unchanged" in n.take()
    clock[0] += 10 * updates.NOTICE_EVERY_S
    assert n.take() is None


def test_version_only_change(tmp_path):
    path = tmp_path / "seen.json"
    updates.record(OLD, "0.1.0", path, now=1000)
    state = updates.record(OLD, "0.1.1", path, now=2000)
    assert "Tools are unchanged" in updates.note_text(state)


def test_results_carry_the_note(monkeypatch):
    notes = iter(["RelayMCP was updated at 20:05: new tools act.", None])
    monkeypatch.setattr(lean, "NOTE_HOOK", lambda: next(notes))

    async def tool():
        return {"ok": True}

    first = json.loads(asyncio.run(lean.lean_result(tool)()))
    second = json.loads(asyncio.run(lean.lean_result(tool)()))
    assert first == {"ok": True, "relaymcp_update": "RelayMCP was updated at 20:05: new tools act."}
    assert second == {"ok": True}


def test_upgrade_from_a_runtime_that_never_recorded_its_tools(tmp_path):
    names = updates.V010_TOOLS + ["act", "behavior"]
    state = updates.record(names, "0.2.0", tmp_path / "seen.json", now=5000, upgraded=True)
    assert state["added"] == ["act", "behavior"] and state["previous_version"] == "0.1.0"
    fresh = updates.record(names, "0.2.0", tmp_path / "other.json", now=5000, upgraded=False)
    assert "changed_at" not in fresh  # a fresh install has nothing to announce
    assert len(updates.V010_TOOLS) == 35
