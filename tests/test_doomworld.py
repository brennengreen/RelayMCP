from pathlib import Path

import pytest


def test_experiments_replay_the_recorded_play(tmp_path, monkeypatch):
    pytest.importorskip("vizdoom")
    from relaymcp.play import doomworld as dw
    monkeypatch.setattr(dw, "BASE", str(Path(__file__).parents[1] / "examples" / "learned_doom_agent.py"))
    monkeypatch.setattr(dw, "SPLITS", {0: ("MAP05",)})
    monkeypatch.setattr(dw, "SEGMENT", 12)
    w = dw.DoomWorld(tmp_path, steps_per_map=12)
    segs = w.record(0)
    assert segs and segs[0]["level"] == "MAP05" and segs[0]["objs"][0][0]["cat"] == "player"
    seg = segs[0]
    replay = w.experiment(seg, 0, seg["actions"])
    assert replay == seg["objs"][:len(replay)] and len(replay) == len(seg["objs"])
    assert dw.walls("MAP05") and "Doom" in w.describe({"actions": w.actions()}, 1)
