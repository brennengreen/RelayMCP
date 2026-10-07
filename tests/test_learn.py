"""spinal learn: a model edits a program from practice; failures become drills; a change is kept only when paired
comparison says it's better. Played here on a tiny game with a scripted 'model', no game engine needed."""

import json
import threading
from pathlib import Path

from relaymcp.play import arena, learn


class Guess:
    """Say the number 4. A level scores how close act(level) gets; a miss is saved as a drill."""
    name = "guess"
    manual = "Say a number from 0 to 10."
    starter = "class Agent:\n    def act(self, obs, t):\n        return 0\n"
    practice = ("3", "5", "7")
    monitor = ("8",)
    held_out = ("9",)
    drill_s = 1

    def _say(self, path, level, log):
        return learn.load_agent(path, log).act(int(level), 0)

    def play(self, path, level, seed=1, bank=None):
        log, moments = learn.Log(), []
        try:
            n = self._say(path, level, log)
            score, error = max(0.0, 1 - abs(n - 4) / 10), None
        except Exception as e:  # noqa: BLE001
            score, error = 0.0, repr(e)
        if bank and score < 1:
            did = f"{level}-miss"
            Path(bank, did + ".state").write_text(level)
            moments.append({"id": did, "kind": "miss", "t": 0.0, "why": f"said not 4 on {level}",
                            "state": did + ".state", "level": level, "seed": seed})
        return {"level": level, "seed": seed, "score": score, "tally": f"said {score}", "events": [],
                "log": log.lines, "error": error, "ms_p95": 0.0, "moments": moments}

    def drills(self, path, level, drills, bank):
        out = []
        for d in drills:
            n = self._say(path, Path(bank, d["state"]).read_text(), learn.Log())
            out.append({"id": d["id"], "score": float(n == 4), "tally": f"said {n}", "events": [], "log": [],
                        "error": None})
        return out


def program(n, extra="", book=None):
    nb = f"```notebook\n{book}\n```\n" if book else ""
    return f"NOTE: say {n}\n{nb}```python\nclass Agent:\n    def act(self, obs, t):\n{extra}        return {n}\n```"


def edit(old, new):
    return f"NOTE: edit\n<<<<<<< SEARCH\n{old}\n=======\n{new}\n>>>>>>> REPLACE\n"


def quiet(*_):
    return None


def test_it_keeps_only_real_gains_and_turns_failures_into_drills(tmp_path):
    replies = iter([program(2, book="4 is not 2"), "no code here", program(9),
                    edit("        return 2", "        log('seen', obs)\n        return 4"),
                    edit("return 99", "return 1")])
    prompts = []

    def model(p):
        prompts.append(p)
        return next(replies)
    st = learn.learn(Guess(), model, tmp_path, rounds=5, width=1, isolate=False, say=quiet, label="scripted")
    verdicts = [t["verdict"].split(":")[0] for t in st["tries"]]
    assert verdicts[:5] == ["the start", "ACCEPTED", "not run (the reply had neither edits nor a program)",
                            "rejected", "ACCEPTED"]
    assert st["tries"][5]["verdict"].startswith("not run (edit 1: its SEARCH text is found 0 times")
    assert st["best"] == 4 and "return 4" in (tmp_path / "agent.py").read_text()
    assert st["notebook"] == "4 is not 2" and "4 is not 2" in prompts[1]  # lessons carry over
    assert "Say a number" in prompts[0] and "drill 3-miss" in prompts[0]  # the starter's misses are drills at once
    assert "[0.0s] seen 3" in prompts[4]  # the program's own log reaches the model
    assert {d["id"] for d in st["drills"]} == {"3-miss", "5-miss", "7-miss"}
    assert st["curve"][-1]["drills_passed"] == 1.0 and st["curve"][-1]["monitor"] == 1.0
    assert [c["round"] for c in st["curve"]] == [0, 1, 2, 3, 4, 5] and st["model_calls"] == 5
    assert st["held_out"]["score"] == 1.0
    saved = json.loads((tmp_path / "learn.json").read_text())
    assert saved["best"] == 4 and saved["game"] == "guess"
    outline = json.loads((tmp_path / "summary.json").read_text())
    assert outline["drill_bank"] == 3 and "drills" not in outline


def test_candidates_run_in_parallel_and_the_biggest_sure_gain_wins(tmp_path):
    lock, replies = threading.Lock(), iter([program(3), program(4), program(9)])

    def model(p):
        with lock:
            return next(replies)
    st = learn.learn(Guess(), model, tmp_path, rounds=1, width=3, isolate=False, say=quiet)
    assert len(st["tries"]) == 4 and "return 4" in (tmp_path / "agent.py").read_text()
    assert sum(t["verdict"].startswith("ACCEPTED") for t in st["tries"]) == 2  # 3 and 4 both beat 0; 4 by more


def test_it_resumes_where_it_stopped(tmp_path):
    learn.learn(Guess(), lambda p: program(3), tmp_path, rounds=1, width=1, isolate=False, say=quiet)
    calls = []
    st = learn.learn(Guess(), lambda p: calls.append(p) or program(4), tmp_path, rounds=2, width=1, isolate=False,
                     say=quiet)
    assert len(calls) == 1 and [t["try"] for t in st["tries"]] == [0, 1, 2] and st["best"] == 2


def test_noise_is_not_a_gain():
    old = {"levels": {"a": 0.5, "b": 0.5, "c": 0.5, "d": 0.5}, "drills": {}}
    mixed = {"levels": {"a": 0.9, "b": 0.1, "c": 0.6, "d": 0.5}, "drills": {}}
    gain, sure = learn.compare(mixed, old)
    assert gain > 0 and sure < 0.9
    better = {"levels": {k: v + 0.1 for k, v in old["levels"].items()}, "drills": {}}
    assert learn.compare(better, old)[1] == 1.0


def test_replies_are_read_for_a_note_a_notebook_and_edits_or_a_program():
    note, book, code, edits = learn.parse("Sure.\nNOTE: aim first\n```notebook\nfacts\n```\n```python\nx = 1\n```")
    assert (note, book, code, edits) == ("aim first", "facts", "x = 1\n", [])
    _, _, code, edits = learn.parse(edit("a = 1", "a = 2") + edit("b", "c"))
    assert code is None and edits == [("a = 1", "a = 2"), ("b", "c")]
    assert learn.apply_edits("a = 1\nb\n", edits) == "a = 2\nc\n"
    try:
        learn.apply_edits("a = 1\na = 1\n", [("a = 1", "x")])
        raise AssertionError
    except ValueError as e:
        assert "found 2 times" in str(e)


def test_practice_and_monitor_never_touch_the_scored_levels():
    atari = learn.GAMES["breakout"]()
    for game in (learn.Doom, atari):
        assert not set(game.practice) & set(game.monitor)
        assert "strateg" not in game.manual.lower()
    assert not (set(learn.Doom.practice) | set(learn.Doom.monitor)) & set(arena.MAPS)
    assert not (set(atari.practice) | set(atari.monitor)) & set(atari.held_out)


def test_a_failure_is_saved_from_a_few_seconds_before_it(tmp_path):
    def save(p):
        Path(p).write_text(str(frame))
    m = learn.Moments(tmp_path, "L", save, ".s", every=10, before=3)
    for frame in range(100):
        m.tick(frame)
    m.keep(95, "died", "fell")
    m.close()
    assert m.found[0]["id"] == "L-died-95" and (tmp_path / "L-died-95.s").read_text() == "60"
    assert not list((tmp_path / "tmp").iterdir())
    off = learn.Moments(None, "L", save, ".s", every=10)
    off.tick(0), off.keep(5, "died", "x"), off.close()  # no folder: records nothing
    assert off.found == []


def test_every_round_practises_on_new_games_and_the_best_plays_them_too(tmp_path):
    played = []

    class Fresh(Guess):
        def units(self, rnd):
            return [(str(10 * rnd + i), 1) for i in range(3)]

        def play(self, path, level, seed=1, bank=None):
            played.append((Path(path).name, level))
            return super().play(path, level, seed, bank)
    st = learn.learn(Fresh(), lambda p: program(3), tmp_path, rounds=2, width=1, isolate=False, say=quiet)
    assert {lv for name, lv in played if name == "try000.py"} >= {"0", "10", "11", "12"}
    assert {lv for name, lv in played if name == "try002.py"} == {"20", "21", "22"}
    assert st["tries"][2]["verdict"].startswith("rejected: practice 90.0% (best 90.0% on the same games)")


def test_levels_and_drills_count_in_their_own_spread():
    old = {"levels": {"a": 0.04, "b": 0.06, "c": 0.05, "d": 0.05}, "drills": {"x": 0.0, "y": 1.0, "z": 1.0, "w": 0.0}}
    # 1 drill more passed (+25 drill points) does not buy 2 level points lost on every level (levels spread about 1 point)
    worse = {"levels": {k: v - 0.02 for k, v in old["levels"].items()}, "drills": {**old["drills"], "x": 1.0}}
    assert learn.compare(worse, old)[0] < 0


def test_small_gains_on_different_problems_are_merged():
    base = "a = 1\nb = 1\nc = 1\nd = 1\ne = 1\nf = 1\ng = 1\n"
    one, two = base.replace("a = 1", "a = 2"), base.replace("g = 1", "g = 3")
    code, whole = learn.merge(base, [one, two, base.replace("a = 1", "a = 9")])
    assert code == base.replace("a = 1", "a = 2").replace("g = 1", "g = 3") and whole == 2  # the clash is left out


class Parts(Guess):
    """Score: a quarter for each of four flags set."""
    practice = tuple(str(i) for i in range(8))
    starter = "A = 0\nB = 0\nC = 0\nD = 0\n\nclass Agent:\n    def act(self, obs, t):\n        return A + B + C + D\n"

    def play(self, path, level, seed=1, bank=None):
        r = super().play(path, level, seed, None)
        n = learn.load_agent(path).act(0, 0)
        return {**r, "score": n / 4}


def test_a_round_merges_candidates_that_each_gained_too_little_alone(tmp_path):
    lock = threading.Lock()
    replies = iter([edit("A = 0", "A = 1"), edit("D = 0", "D = 1"), "no code"])

    def model(p):
        with lock:
            return next(replies)
    st = learn.learn(Parts(), model, tmp_path, rounds=1, width=3, isolate=False, say=quiet)
    merged = st["tries"][-1]
    edits = [t["try"] for t in st["tries"] if t["note"] == "edit"]  # the threads take the replies in any order
    assert sorted(merged["merged"]) == sorted(edits) and len(edits) == 2 and st["best"] == merged["try"]
    assert learn.load_agent(tmp_path / "agent.py").act(0, 0) == 2
