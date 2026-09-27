"""Process sessions, with real child processes (a small Python program standing in for a server console)."""

import json
import sys
import time

import pytest

from relaymcp.device import procs

CONSOLE = (
    "import sys, time\n"
    "print('Starting...', flush=True)\n"
    "time.sleep(0.3)\n"
    "print('Server started.', flush=True)\n"
    "for line in sys.stdin:\n"
    "    cmd = line.strip()\n"
    "    if cmd == 'stop':\n"
    "        print('Stopping', flush=True); break\n"
    "    if cmd == 'list':\n"
    "        print('There are 2 players online:', flush=True); time.sleep(0.1); print('Steve, Alex', flush=True)\n"
    "    elif cmd == 'spam':\n"
    "        [print(f'line {i}', flush=True) for i in range(200)]\n"
    "    else:\n"
    "        print('Unknown command: ' + cmd, flush=True)\n"
)


@pytest.fixture
def console(tmp_path):
    script = tmp_path / "console.py"
    script.write_text(CONSOLE)
    m = procs.Manager(tmp_path / "procs.json")
    yield m, f'"{sys.executable}" "{script}"', tmp_path
    m.stop_all()


def test_start_waits_for_ready_and_lists_itself(console):
    m, cmd, tmp = console
    out = procs.run(m, "start", "srv", cmd, pattern=r"Server started", timeout=10)
    assert out["ready"] is True and out["running"] and "Server started." in out["output"]
    state = json.loads((tmp / "procs.json").read_text())
    assert [s["name"] for s in state["running"]] == ["srv"]
    with pytest.raises(RuntimeError, match="already running"):
        procs.run(m, "start", "srv", cmd)


def test_send_returns_the_whole_reply(console):
    m, cmd, _ = console
    procs.run(m, "start", "srv", cmd, pattern="started")
    out = procs.run(m, "send", "srv", text="list")
    assert out["output"] == ["There are 2 players online:", "Steve, Alex"]
    out = procs.run(m, "send", "srv", text="bogus", pattern="Unknown command")
    assert out["matched"] == "Unknown command: bogus"


def test_read_only_returns_new_lines_and_caps_them(console):
    m, cmd, _ = console
    procs.run(m, "start", "srv", cmd, pattern="started")
    assert procs.run(m, "read", "srv")["output"] == []
    procs.run(m, "send", "srv", text="spam", pattern="line 199")
    out = procs.run(m, "read", "srv", cursor=0, max_lines=10)
    assert out["output"][-1] == "line 199" and len(out["output"]) == 10 and out["skipped"] > 0


def test_wait_times_out_cleanly(console):
    m, cmd, _ = console
    procs.run(m, "start", "srv", cmd, pattern="started")
    t = time.monotonic()
    out = procs.run(m, "wait", "srv", pattern="never printed", timeout=0.5)
    assert out["matched"] is None and out["timed_out"] and time.monotonic() - t < 3


def test_graceful_stop_then_forgotten_from_state(console):
    m, cmd, tmp = console
    procs.run(m, "start", "srv", cmd, pattern="started")
    out = procs.run(m, "stop", "srv", text="stop", timeout=5)
    assert out["stopped"] and out["exit_code"] == 0 and "Stopping" in out["output"]
    assert json.loads((tmp / "procs.json").read_text())["running"] == []
    assert procs.run(m, "list")["sessions"][0]["running"] is False
    procs.run(m, "start", "srv", cmd, pattern="started")  # the name can be reused once it has exited


def test_hard_stop_and_errors(console):
    m, cmd, _ = console
    procs.run(m, "start", "srv", cmd, pattern="started")
    assert procs.run(m, "stop", "srv", timeout=1)["stopped"]
    with pytest.raises(KeyError, match="no process session"):
        procs.run(m, "read", "nope")
    with pytest.raises(ValueError):
        procs.run(m, "start", "bad name!", cmd)
    with pytest.raises(ValueError):
        procs.run(m, "wait", "srv")
    with pytest.raises(RuntimeError, match="has exited"):
        procs.run(m, "send", "srv", text="list")


def test_busy_counts_process_sessions():
    from relaymcp.host import busy
    quiet = {"now": "2026-09-26T21:00:00", "idle_s": 900}
    assert busy.reasons({**quiet, "proc_sessions": "bds"}) == ["process sessions running: bds"]
