"""Game telemetry: RELAY lines parsed into RelayMCP's conventions, and a follower that tracks the newest log file."""

import json
import threading
import time

import pytest

from relaymcp.device import inbox, telemetry


def test_relay_lines_become_samples_in_relaymcp_conventions():
    t = telemetry.Telemetry()
    assert t.feed("[Scripting][inform]-some other message") is None
    line = '2026-09-27 16:30:01:123 [Scripting][inform]-RELAY ' + json.dumps(
        {"t": 1000.0, "tick": 7, "x": 1.5, "y": -60.0, "z": 2.5, "ey": -58.38, "yaw": 90.0, "pitch": 30.0})
    s = t.feed(line, recv_perf=10.0, recv_wall=1.05)
    assert s["pitch"] == -30.0 and s["yaw"] == 90.0 and s["x"] == 1.5  # Minecraft's pitch is down +; ours is up +
    assert s["latency_ms"] == 50.0 and t.samples == 1
    assert t.feed("RELAY {not json") is None and t.bad == 1


def test_latest_ages_and_history_is_kept_for_a_while():
    t = telemetry.Telemetry(keep_s=0.25)
    for i in range(5):
        t.feed("RELAY " + json.dumps({"tick": i}), recv_perf=time.perf_counter() - 0.5 + i * 0.1)
    assert [s["tick"] for s in t.history] == [2, 3, 4]  # older than keep_s behind the newest: gone
    assert t.get()["tick"] == 4 and t.get()["age_ms"] >= 50


def test_the_follower_skips_old_lines_then_reads_new_ones_and_newer_logs(tmp_path):
    old = tmp_path / "ContentLog__1.txt"
    old.write_text("RELAY {\"tick\": 1}\n")
    got, switched = [], []
    f = telemetry.Follower([tmp_path], "ContentLog*.txt", got.append, poll_s=0.002, rescan_s=0.05,
                           on_switch=switched.append)
    f.start()
    try:
        time.sleep(0.15)
        with open(old, "a") as fh:
            fh.write("RELAY {\"tick\": 2}\nRELAY {\"tick\": 3")  # the last line isn't complete yet
        time.sleep(0.1)
        assert got == ['RELAY {"tick": 2}']
        with open(old, "a") as fh:
            fh.write("}\r\n")
        time.sleep(0.1)
        assert got[-1] == 'RELAY {"tick": 3}'
        newer = tmp_path / "ContentLog__2.txt"
        time.sleep(0.02)
        newer.write_text("RELAY {\"tick\": 10}\n")  # a new game session: read from its start
        deadline = time.time() + 2
        while time.time() < deadline and got[-1] != 'RELAY {"tick": 10}':
            time.sleep(0.02)
        assert got[-1] == 'RELAY {"tick": 10}' and switched[-1].endswith("ContentLog__2.txt")
    finally:
        f.stop()


def test_the_inbox_keeps_a_stream_as_latest_state_without_flooding_events():
    box = inbox.Inbox()
    box.post("server", {"event": "joined"})
    for i in range(50):
        box.set("minecraft", {"tick": i})
    r = box.read()
    assert r["latest"]["minecraft"] == {"tick": 49} and [e["topic"] for e in r["events"]] == ["server"]


def test_programs_read_telemetry_and_a_fresh_sample_counts_as_a_look():
    np = pytest.importorskip("numpy")
    from relaymcp.device import behave
    t = telemetry.Telemetry()

    def feed():
        for i in range(30):
            t.feed("RELAY " + json.dumps({"tick": i, "yaw": 10.0 + i, "pitch": -5.0}))
            time.sleep(0.01)

    frame = np.zeros((60, 80, 4), np.uint8)

    class Out:
        def hold(self, state):
            pass

        def release(self, used=()):
            pass

    rt = behave.Runtime(lambda region: (frame, time.perf_counter()), Out(), telemetry=t.get)
    feeder = threading.Thread(target=feed)
    feeder.start()
    code = ("pad(ls=(0, 1))\n"
            "until(lambda: (telemetry() or {}).get('tick', -1) >= 25, timeout=2, hz=100)\n"
            "s = telemetry()\n"
            "pad()\n"
            "result = {'tick': s['tick'], 'pitch': s['pitch']}\n")
    out = behave.run_tool(rt, "start", "program", {"code": code, "wait": True}, max_s=5)
    feeder.join()
    assert out["result"]["tick"] >= 25 and out["result"]["pitch"] == 5.0, out
    assert out["cadence"]["worst_ms"] < 150, out["cadence"]  # telemetry reads were its looks (no frame reads)


def test_command_replies_are_events_not_the_pose():
    got = []
    t = telemetry.Telemetry(on_reply=got.append)
    t.feed("RELAY " + json.dumps({"tick": 1, "yaw": 5.0, "pitch": 0.0}))
    assert t.feed("RELAY " + json.dumps({"reply": "blocks", "blocks": [[0, 1, 2, "oak_planks"]]})) is None
    assert t.get()["tick"] == 1 and got == [{"reply": "blocks", "blocks": [[0, 1, 2, "oak_planks"]]}]
