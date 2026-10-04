"""The Spinal Score and its leaderboards: Doom's own tally, entries recomputed from it (a malformed or miscounted pull
request fails here), the season's totals checked against the game, and the site data rendered."""

import json
from pathlib import Path

import pytest

from relaymcp.play import arena

REPO = Path(__file__).resolve().parents[1]


def entry(board="reflex", name="Test", kills=7):
    maps = [{"map": m, "kills": kills, "secrets": 0, "exited": False, "seconds": 20.0, "died": True, "missed_tics": 3}
            for m in arena.MAPS]
    for p in maps:
        p["score"] = round(100 * arena.map_score(p["map"], p["kills"], 0, False, 20.0)[3], 1)
    score = round(100 * sum(arena.map_score(m, kills, 0, False, 20.0)[3] for m in arena.MAPS) / len(arena.MAPS), 1)
    return {"board": board, "name": name, "agent": "planner", "model": None, "scenario": arena.SCENARIO, "score": score,
            "maps": maps, "machine": "Darwin arm64", "date": "2026-10-04",
            "metrics": {"kills": 10.0, "secrets": 0.0, "exit": 0.0, "exits": 0, "deaths": 4, "decision_ms_p50": 0.01,
                        "decision_ms_p95": 0.02, "missed_tics": 12, "on_intent": None, "compile_s": None}}


def test_a_map_is_scored_with_dooms_tally():
    assert arena.map_score("MAP01", 28, 3, True, 30.0) == (1.0, 1.0, 1.0, 1.0)  # UV-Max under par: 100%
    k, s, e, total = arena.map_score("MAP01", 28, 3, True, 60.0)  # an expert maxing at twice par
    assert e == 0.5 and round(total, 3) == 0.833
    assert arena.map_score("MAP01", 40, 9, False, 90.0) == (1.0, 1.0, 0.0, 2 / 3)  # capped; no exit, no exit credit
    assert arena.map_score("MAP02", 0, 0, False, 5.0)[3] == 0.0


def test_the_season_totals_are_the_games():
    pytest.importorskip("vizdoom")
    assert arena.levels() == arena.SEASON


def test_the_layout_comes_from_the_map_and_is_walkable():
    """ViZDoom's sectors stream crashes on MAP03+, so the layout is read from the map: the start must be inside it."""
    pytest.importorskip("vizdoom")
    import struct

    from relaymcp.play import doom
    from relaymcp.play.nav import NavGrid
    for m in arena.MAPS:
        things = doom.map_data(m)["THINGS"]
        x, y = next(struct.unpack_from("<hh", things, j) for j in range(0, len(things), 10)
                    if struct.unpack_from("<h", things, j + 6)[0] == 1)  # player 1 start
        nav = NavGrid(doom.layout(m))
        reach = nav.reachable(x, y)
        assert reach is not None and 50 < reach.sum() < reach.size / 2, m  # a real room, not the void around it


def test_the_repo_entries_are_valid():
    arena.load(REPO / "leaderboards")


def test_boards_rank_by_score_and_render_the_site_data(tmp_path):
    for i, k in enumerate((3, 20)):
        (tmp_path / "lb" / "reflex").mkdir(parents=True, exist_ok=True)
        (tmp_path / "lb" / "reflex" / f"e{i}.json").write_text(json.dumps(entry(name=f"E{i}", kills=k)))
    (tmp_path / "site").mkdir()
    readme = tmp_path / "README.md"
    readme.write_text("x\n<!-- leaderboard:start -->\nold\n<!-- leaderboard:end -->\ny\n")
    arena.render(tmp_path / "lb", tmp_path / "LB.md", readme, tmp_path / "site")
    board = (tmp_path / "LB.md").read_text()
    assert board.index("| 1 | E1 |") < board.index("| 2 | E0 |") and arena.SCALE in board
    text = readme.read_text()
    assert "old" not in text and "| 1 | E1 |" in text and text.startswith("x\n") and text.endswith("y\n")
    site = json.loads((tmp_path / "site" / "leaderboard.json").read_text())
    assert site["scale"] == arena.SCALE and [r["name"] for r in site["boards"]["reflex"]["rows"]] == ["E1", "E0"]


@pytest.mark.parametrize("change", [
    lambda e: e.pop("maps"),
    lambda e: e.update(scenario="doom-deathmatch: 5 x 60 s"),  # last season's test, or another one
    lambda e: e.update(score=e["score"] + 5),  # a score its tally does not add up to
    lambda e: e["maps"].pop(),  # not every map
])
def test_a_malformed_or_miscounted_entry_is_refused(tmp_path, change):
    e = entry()
    change(e)
    (tmp_path / "reflex").mkdir()
    (tmp_path / "reflex" / "bad.json").write_text(json.dumps(e))
    with pytest.raises(ValueError):
        arena.load(tmp_path)
