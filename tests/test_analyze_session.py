import importlib.util
import json
import sqlite3
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "analyze_session.py"
spec = importlib.util.spec_from_file_location("analyze_session", SCRIPT)
analyze_session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyze_session)


def _db(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE assistant_usage_events (id INTEGER PRIMARY KEY, session_id TEXT, model TEXT, input_tokens "
                "INTEGER, duration_ms INTEGER, time_to_first_token_ms INTEGER, reasoning_effort TEXT, created_at TEXT)")
    con.executemany("INSERT INTO assistant_usage_events (session_id, model, input_tokens, duration_ms, "
                    "time_to_first_token_ms, reasoning_effort, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()


def test_model_calls_bucket_first_token_by_context(tmp_path):
    db = tmp_path / "store.db"
    _db(db, [("s1", "opus", 50_000, 6000, 5000, "max", "2026-09-27 01:00:00"),
             ("s1", "opus", 500_000, 20000, 13000, "max", "2026-09-27 01:05:00"),
             ("s1", "opus", 480_000, 16000, 11000, "max", "2026-09-27 01:06:00"),
             ("other", "haiku", 10_000, 1000, 500, None, "2026-09-27 01:00:00")])
    m = analyze_session.model_calls(str(db), "s1")
    assert m["calls"] == 3 and m["model_min"] == round(42 / 60, 1) and m["context_tokens_max"] == 500_000
    assert m["ttft_by_context"] == {"<100k": {"n": 1, "median_s": 5.0}, ">450k": {"n": 2, "median_s": 12.0}}
    assert m["models"] == ["opus/max"]
    utc = __import__("datetime").datetime(2026, 9, 27, 1, 4, tzinfo=__import__("datetime").timezone.utc).timestamp()
    assert analyze_session.model_calls(str(db), "s1", since=utc)["calls"] == 2
    assert analyze_session.model_calls(str(tmp_path / "missing.db"), "s1").get("error")


def test_episode_window_and_device_steps(tmp_path):
    events = tmp_path / "events.jsonl"
    lines = [
        {"type": "assistant.message", "timestamp": "2026-09-27T01:00:00Z",
         "data": {"toolRequests": [{"toolCallId": "a", "name": "ally-handheld-gamepad_press"}]}},
        {"type": "tool.execution_start", "timestamp": "2026-09-27T01:00:01Z", "data": {"toolCallId": "a", "toolName": "ally-handheld-gamepad_press"}},
        {"type": "tool.execution_complete", "timestamp": "2026-09-27T01:00:03Z", "data": {"toolCallId": "a", "result": {"content": "ok"}}},
        {"type": "assistant.message", "timestamp": "2026-09-27T01:00:13Z",
         "data": {"toolRequests": [{"toolCallId": "b", "name": "ally-handheld-observe"}]}},
        {"type": "tool.execution_start", "timestamp": "2026-09-27T01:00:13Z", "data": {"toolCallId": "b", "toolName": "ally-handheld-observe"}},
        {"type": "tool.execution_complete", "timestamp": "2026-09-27T01:00:14Z", "data": {"toolCallId": "b", "result": {"content": "x"}}},
    ]
    events.write_text("\n".join(json.dumps(x) for x in lines))
    report = analyze_session.analyze(str(events))
    assert report["device_steps"] == 1 and report["model_s_median"] == 10.0 and report["device_s_median"] == 1.0
    first, last = analyze_session.episode_window(str(events), None, None)
    assert last - first == 14
