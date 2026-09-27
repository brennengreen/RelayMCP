"""Behaviors against a simulated screen (numpy frames) and recorded outputs: no Windows, no real input."""

import os
import sys
import threading
import time

import pytest

# Behaviors run on Windows handhelds. Shared macOS CI machines oversleep by 15-25 ms, so strict timing is only
# checked on Windows and on developer machines.
SLOPPY_CLOCK = sys.platform == "darwin" and bool(os.environ.get("CI"))

np = pytest.importorskip("numpy")

from relaymcp.device import behave  # noqa: E402


class World:
    """A 400x300 screen: a colored square the tests move around, a flash panel, and a cursor that mouse moves push."""

    def __init__(self):
        self.lock = threading.Lock()
        self.target = [300.0, 200.0]
        self.flash = False
        self.cursor = [50.0, 50.0]
        self.keys, self.pads, self.clicks, self.moves, self.sticks, self.released = [], [], 0, [], [], 0
        self.text = []

    def frame(self, region=None):
        with self.lock:
            img = np.zeros((300, 400, 4), np.uint8)
            x, y = int(self.target[0]), int(self.target[1])
            img[max(0, y - 6):y + 6, max(0, x - 6):x + 6] = (0, 0, 255, 255)  # BGRA red square
            if self.flash:
                img[0:40, 0:40] = (0, 255, 0, 255)  # green panel top-left
        if region:
            img = img[region[1]:region[3], region[0]:region[2]]
        return img, time.perf_counter()


class Outputs:
    def __init__(self, world):
        self.w = world

    def key(self, keys):
        self.w.keys.append(tuple(keys))

    def pad(self, buttons):
        self.w.pads.append(tuple(buttons))

    def click(self):
        self.w.clicks += 1

    def mouse(self, dx, dy):
        with self.w.lock:
            self.w.cursor[0] += dx
            self.w.cursor[1] += dy
        self.w.moves.append((dx, dy))

    def stick(self, side, x, y):
        self.w.sticks.append((side, round(x, 2), round(y, 2)))

    def release(self, used=()):
        self.w.released += 1
        self.w.last_used = set(used)


@pytest.fixture
def world():
    return World()


@pytest.fixture
def rt(world):
    runtime = behave.Runtime(world.frame, Outputs(world), read_text=lambda region: world.text,
                             cursor=lambda: list(world.cursor))
    yield runtime
    runtime.stop()


def wait_state(rt, rid, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        s = rt.status(rid)
        if s["state"] != "running":
            return s
        time.sleep(0.02)
    return rt.status(rid)


def test_react_to_a_color_quickly_and_only_once(rt, world):
    rid = rt.start("react", {"region": [0, 0, 40, 40], "when": {"color": [0, 255, 0], "tol": 30},
                             "do": {"key": ["space"]}, "repeat": False}, max_s=3)["id"]
    time.sleep(0.1)
    assert world.keys == []
    world.flash = True
    s = wait_state(rt, rid)
    assert s["state"] == "done" and s["reason"] == "reacted" and world.keys == [("space",)]
    reacted = [e for e in s["new_events"] if e["event"] == "reacted"][0]
    assert reacted["ms_from_frame"] < 50 and reacted["fraction"] > 0.9
    assert world.released >= 1  # input released when it ended


def test_react_on_change_with_cooldown(rt, world):
    rid = rt.start("react", {"region": [0, 0, 40, 40], "when": {"change": 20}, "do": {"pad": ["a"]},
                             "cooldown_ms": 200}, max_s=1.2)["id"]
    time.sleep(0.15)
    world.flash = True
    time.sleep(0.4)
    world.flash = False
    s = wait_state(rt, rid)
    assert s["state"] == "done" and "time limit" in s["reason"] and world.pads == [("a",), ("a",)]


def test_track_moves_the_cursor_onto_a_moving_target(rt, world):
    rid = rt.start("track", {"color": [255, 0, 0], "tol": 40, "aim": "cursor", "output": "mouse", "gain": 0.7,
                             "follow": True}, max_s=2.5)["id"]

    def move():
        t0 = time.time()
        while time.time() - t0 < 2.4:
            a = (time.time() - t0) * 2.0
            with world.lock:
                world.target = [200 + 120 * np.cos(a), 150 + 90 * np.sin(a)]
            time.sleep(0.01)

    mover = threading.Thread(target=move)
    mover.start()
    mover.join()
    s = wait_state(rt, rid)
    assert s["state"] == "done" and s["hz"] > (10 if SLOPPY_CLOCK else 30)
    ex, ey = world.cursor[0] - world.target[0], world.cursor[1] - world.target[1]
    assert (ex * ex + ey * ey) ** 0.5 < 30, (world.cursor, world.target)
    assert s["error_px_p50"] < 30


def test_track_with_a_stick_points_toward_the_target(rt, world):
    world.target = [350.0, 60.0]  # right of and above a fixed crosshair at the screen center
    rid = rt.start("track", {"color": [255, 0, 0], "aim": [200, 150], "output": "right_stick"}, max_s=0.3)["id"]
    wait_state(rt, rid)
    side, x, y = world.sticks[0]
    assert side == "right_stick" and x > 0 and y > 0  # right, and up is positive


def test_press_until_text_appears(rt, world):
    rid = rt.start("press_until", {"do": {"pad": ["dpad_down"]}, "every_ms": 50, "until": {"text": "Play Online"},
                                   "max_presses": 50}, max_s=5)["id"]

    def later():
        time.sleep(0.3)
        world.text = [{"text": "Play  online", "box": [0, 0, 1, 1]}]

    threading.Thread(target=later).start()
    s = wait_state(rt, rid)
    assert s["state"] == "done" and 3 <= len(world.pads) <= 12 and s["presses"] == len(world.pads)


def test_press_until_gives_up(rt, world):
    rid = rt.start("press_until", {"do": {"key": ["down"]}, "every_ms": 20, "until": {"text": "never"},
                                   "max_presses": 5}, max_s=5)["id"]
    s = wait_state(rt, rid)
    assert s["state"] == "stopped" and "gave up after 5" in s["reason"] and len(world.keys) == 5


def test_watch_reports_each_appearance(rt, world):
    rid = rt.start("watch", {"region": [0, 0, 40, 40], "when": {"color": [0, 255, 0]}, "every_ms": 20}, max_s=1.0)["id"]
    for on in (True, False, True):
        time.sleep(0.15)
        world.flash = on
    s = wait_state(rt, rid)
    assert [e["event"] for e in s["new_events"]].count("seen") == 2 and world.keys == []


def test_stop_takeover_limits_and_errors(world):
    who = {"reason": None}
    rt = behave.Runtime(world.frame, Outputs(world), takeover=lambda: who["reason"])
    rid = rt.start("watch", {"region": [0, 0, 40, 40], "when": {"change": 5}}, max_s=30)["id"]
    time.sleep(0.1)
    who["reason"] = "controller slot 0"
    s = wait_state(rt, rid)
    assert s["state"] == "stopped" and "you took over" in s["reason"] and world.released >= 1
    who["reason"] = None
    rid = rt.start("watch", {"when": {"change": 5}}, max_s=30)["id"]
    time.sleep(0.05)
    assert rt.stop(rid) == [rid] and rt.status(rid)["state"] == "stopped"
    ids = [rt.start("watch", {"when": {"change": 5}}, max_s=5)["id"] for _ in range(behave.MAX_RUNS)]
    with pytest.raises(RuntimeError, match="at most"):
        rt.start("watch", {"when": {"change": 5}})
    rt.stop()
    assert all(rt.status(i)["state"] == "stopped" for i in ids)
    bad = rt.start("react", {"when": {}, "do": {"key": ["x"]}}, max_s=2)["id"]
    s = wait_state(rt, bad)
    assert s["state"] == "failed" and "color" in s["reason"]
    with pytest.raises(ValueError):
        rt.start("dance", {})
    assert behave.run_tool(rt, "kinds")["kinds"].keys() == behave.KINDS.keys()


@pytest.mark.skipif(SLOPPY_CLOCK, reason="shared macOS CI machines oversleep")
def test_sleep_until_is_precise():
    lateness = []
    with behave.HiResTimer():  # as behaviors and pad sequences run
        for _ in range(40):
            target = time.perf_counter() + 0.005
            behave.sleep_until(target)
            lateness.append((time.perf_counter() - target) * 1000)
    s = sorted(lateness)
    assert s[len(s) // 2] < 1.0 and s[int(len(s) * 0.9)] < 4.0, lateness  # shared CI machines hiccup now and then


class Menu:
    """Five rows; the highlighted one has a bright background. d-pad up/down (or arrows) move it; A/Enter selects."""

    ITEMS = ["Play", "Marketplace", "Settings", "Profile", "Quit"]

    def __init__(self):
        self.index, self.selected, self.presses = 0, None, []

    def lines(self, region=None):
        return [{"text": t, "box": [20, 20 + 40 * i, 200, 50 + 40 * i]} for i, t in enumerate(self.ITEMS)]

    def frame(self, region=None):
        img = np.full((300, 400, 4), 40, np.uint8)
        y = 20 + 40 * self.index
        img[y - 5:y + 35, 10:210] = (200, 120, 30, 255)  # BGRA: a blue-ish highlight bar
        return img, time.perf_counter()

    def press(self, names):
        name = names[0]
        self.presses.append(name)
        if name in ("dpad_down", "down"):
            self.index = min(len(self.ITEMS) - 1, self.index + 1)
        elif name in ("dpad_up", "up"):
            self.index = max(0, self.index - 1)
        elif name in ("a", "enter"):
            self.selected = self.ITEMS[self.index]


class MenuOutputs:
    def __init__(self, menu):
        self.m = menu

    def pad(self, buttons):
        self.m.press(buttons)

    def key(self, keys):
        self.m.press(keys)

    def release(self, used=()):
        pass


def test_highlight_detection():
    menu = Menu()
    menu.index = 3
    frame, _ = menu.frame()
    assert behave.highlighted(menu.lines(), frame)["text"] == "Profile"
    flat = np.full((300, 400, 4), 40, np.uint8)
    assert behave.highlighted(menu.lines(), flat) is None  # nothing stands out
    assert behave.best_line(menu.lines(), "market")["text"] == "Marketplace"


@pytest.mark.parametrize("with_", ["pad", "keys"])
def test_navigate_reaches_and_confirms(with_):
    menu = Menu()
    rt = behave.Runtime(menu.frame, MenuOutputs(menu), read_text=menu.lines)
    rid = rt.start("navigate", {"text": "profile", "with": with_, "confirm": True, "settle_ms": 20}, max_s=5)["id"]
    s = wait_state(rt, rid)
    assert s["state"] == "done" and menu.selected == "Profile", s
    assert s["moves"] == 3 and len(menu.presses) == 4  # three downs, then confirm
    menu2 = Menu()
    menu2.index = 4
    rt2 = behave.Runtime(menu2.frame, MenuOutputs(menu2), read_text=menu2.lines)
    s2 = wait_state(rt2, rt2.start("navigate", {"text": "Play", "settle_ms": 20}, max_s=5)["id"])
    assert s2["state"] == "done" and menu2.index == 0 and set(menu2.presses) == {"dpad_up"}


def test_navigate_gives_up_on_a_missing_item():
    menu = Menu()
    rt = behave.Runtime(menu.frame, MenuOutputs(menu), read_text=menu.lines)
    s = wait_state(rt, rt.start("navigate", {"text": "Credits", "max_moves": 4, "settle_ms": 20}, max_s=5)["id"])
    assert s["state"] == "stopped" and "didn't reach 'credits' in 4 moves" in s["reason"]


def test_script_runs_states_until_text_then_inbox_state(world):
    from relaymcp.device import inbox
    box = inbox.Inbox()
    rt = behave.Runtime(world.frame, Outputs(world), read_text=lambda region: world.text,
                        state_events=lambda topic, since: (lambda r: (r["events"], r["cursor"]))(box.read(topic, since, 200)))
    box.post("mob", {"type": "iron_golem", "angry": True})  # before the script: must not count
    script = {"start": "find", "states": {
        "find": {"do": {"pad": ["dpad_right"]}, "every_ms": 30, "until": {"text": "Iron Golem"}, "next": "attack"},
        "attack": {"do": {"pad": ["rt"]}, "every_ms": 30, "until": {"state": {"topic": "mob", "match": "angry=true"}},
                   "next": "cool_down"},
        "cool_down": {"ms": 100, "next": "done"}}}
    rid = rt.start("script", script, max_s=5)["id"]

    def game():
        time.sleep(0.2)
        world.text = [{"text": "Iron Golem", "box": [0, 0, 1, 1]}]
        time.sleep(0.25)
        box.post("mob", {"type": "iron_golem", "angry": True})

    threading.Thread(target=game).start()
    s = wait_state(rt, rid)
    assert s["state"] == "done" and s["reason"] == "script finished", s
    assert [e["name"] for e in s["new_events"] if e["event"] == "state"] == ["find", "attack", "cool_down"]
    assert ("dpad_right",) in world.pads and ("rt",) in world.pads
    rights, rts = world.pads.count(("dpad_right",)), world.pads.count(("rt",))
    assert 3 <= rights <= 12 and 4 <= rts <= 16, (rights, rts)


def test_script_timeouts_branch_or_stop(rt, world):
    s = wait_state(rt, rt.start("script", {"start": "a", "states": {
        "a": {"until": {"text": "never"}, "timeout_s": 0.2, "on_timeout": "b"},
        "b": {"ms": 50, "next": "done"}}}, max_s=5)["id"])
    assert s["state"] == "done" and [e["name"] for e in s["new_events"] if e["event"] == "state"] == ["a", "b"]
    s = wait_state(rt, rt.start("script", {"states": {"a": {"until": {"text": "never"}, "timeout_s": 0.2}}}, max_s=5)["id"])
    assert s["state"] == "stopped" and "timed out" in s["reason"]
    s = wait_state(rt, rt.start("script", {"start": "x", "states": {"x": {"ms": 1, "next": "x"}}}, max_s=10)["id"], 15)
    assert s["state"] == "stopped" and "200 states" in s["reason"]
    s = wait_state(rt, rt.start("script", {"start": "nope", "states": {}}, max_s=2)["id"])
    assert s["state"] == "failed"


def test_track_finishes_on_target_unless_following(rt, world):
    world.target = [60.0, 60.0]  # right next to the cursor
    s = wait_state(rt, rt.start("track", {"color": [255, 0, 0], "aim": "cursor", "within": 15}, max_s=5)["id"])
    assert s["state"] == "done" and s["reason"] == "on target" and s["seconds"] < 2


def test_slow_intervals_are_honored_and_stop_is_immediate(rt, world):
    rid = rt.start("press_until", {"do": {"key": ["x"]}, "every_ms": 1500, "until": {"text": "never"},
                                   "max_presses": 10}, max_s=30)["id"]
    time.sleep(2.0)
    assert len(world.keys) == 2  # at 0 s and 1.5 s, not once a second
    t0 = time.monotonic()
    rt.stop(rid)
    assert time.monotonic() - t0 < 1.0 and rt.status(rid)["state"] == "stopped"


class TwoLineMenu(Menu):
    ITEMS = ["Resume", "Quit"]


def test_two_line_menus_find_the_real_highlight_and_never_guess():
    menu = TwoLineMenu()
    menu.index = 1  # Quit highlighted
    frame, _ = menu.frame()
    assert behave.highlighted(menu.lines(), frame)["text"] == "Quit"
    rt = behave.Runtime(menu.frame, MenuOutputs(menu), read_text=menu.lines)
    s = wait_state(rt, rt.start("navigate", {"text": "Resume", "confirm": True, "settle_ms": 20}, max_s=5)["id"])
    assert s["state"] == "done" and menu.selected == "Resume" and menu.presses == ["dpad_up", "a"]
    flat = np.full((300, 400, 4), 40, np.uint8)
    assert behave.highlighted(menu.lines(), flat) is None
    one = [{"text": "Continue", "box": [20, 20, 200, 50]}]
    lit = flat.copy()
    lit[15:55, 10:210] = (200, 120, 30, 255)
    assert behave.highlighted(one, lit)["text"] == "Continue" and behave.highlighted(one, flat) is None


def test_every_item_on_a_button_background_still_finds_the_odd_one_out():
    menu = Menu()
    menu.index = 2

    def frame(region=None):
        img = np.full((300, 400, 4), 10, np.uint8)
        for i in range(len(menu.ITEMS)):
            y = 20 + 40 * i
            img[y - 5:y + 35, 10:210] = (90, 90, 90, 255) if i != menu.index else (220, 220, 220, 255)
        return img, time.perf_counter()

    assert behave.highlighted(menu.lines(), frame()[0])["text"] == "Settings"


def test_long_intervals_respect_the_time_limit_and_takeover(world):
    who = {"reason": None}
    rt = behave.Runtime(world.frame, Outputs(world), takeover=lambda: who["reason"], read_text=lambda r: [])
    t0 = time.monotonic()
    s = wait_state(rt, rt.start("press_until", {"do": {"key": ["x"]}, "every_ms": 5000, "until": {"text": "never"}},
                                max_s=1.0)["id"], 10)
    assert s["state"] == "done" and time.monotonic() - t0 < 2.0  # not 5 s: the limit cuts the wait short
    rid = rt.start("press_until", {"do": {"key": ["x"]}, "every_ms": 4000, "until": {"text": "never"}}, max_s=30)["id"]
    time.sleep(0.3)
    t1 = time.monotonic()
    who["reason"] = "controller slot 0"
    s = wait_state(rt, rid, 5)
    assert s["state"] == "stopped" and "took over" in s["reason"] and time.monotonic() - t1 < 1.0


def test_script_repeats_on_schedule(rt, world):
    s = wait_state(rt, rt.start("script", {"states": {"a": {"do": {"key": ["x"]}, "every_ms": 100, "ms": 1000}}},
                                max_s=5)["id"])
    assert s["state"] == "done" and 9 <= len(world.keys) <= 11, len(world.keys)


def _texture(h, w, block=24, seed=1):
    rng = np.random.default_rng(seed)
    cells = rng.integers(0, 255, size=(h // block + 1, w // block + 1), dtype=np.uint8)
    img = np.kron(cells, np.ones((block, block), np.uint8))[:h, :w]
    return np.dstack([img, (img * 7) % 255, (img * 3) % 255, np.full_like(img, 255)])


def test_match_template_finds_a_patch_again():
    frame = _texture(1080, 1920)
    img = behave.gray_small(frame)
    tmpl = img[100:130, 200:230]
    x, y, score = behave.match_template(img, tmpl)
    assert (x, y) == (200, 100) and score > 0.99
    shifted = behave.gray_small(np.roll(frame, 160, axis=1))  # the view turned by 160 px
    x2, y2, score2 = behave.match_template(shifted, tmpl)
    assert (x2, y2) == (240, 100) and score2 > 0.99


class PanWorld:
    """A camera over a wide panorama: the right stick turns it (px/s at full deflection), frames are 1920x1080."""

    def __init__(self):
        self.pano = _texture(1080, 6000, seed=7)
        self.x, self.v, self.t = 2000.0, 0.0, time.perf_counter()
        self.sticks, self.lock = [], threading.Lock()

    def _advance(self):
        now = time.perf_counter()
        self.x += self.v * (now - self.t) * 900
        self.t = now

    def frame(self, region=None):
        with self.lock:
            self._advance()
            x = int(self.x)
            return self.pano[:, x:x + 1920].copy(), time.perf_counter()

    def stick(self, side, x, y):
        with self.lock:
            self._advance()
            self.v = x
            self.sticks.append((round(x, 2), round(y, 2)))


class PanOutputs:
    def __init__(self, w):
        self.w = w

    def stick(self, side, x, y):
        self.w.stick(side, x, y)

    def release(self, used=()):
        self.w.stick("right_stick", 0.0, 0.0)


def test_track_at_a_point_turns_until_it_is_under_the_crosshair():
    world = PanWorld()
    rt = behave.Runtime(world.frame, PanOutputs(world))
    start_x = world.x
    s = wait_state(rt, rt.start("track", {"at": [1500, 540], "within": 24}, max_s=8)["id"], 12)
    assert s["state"] == "done" and s["reason"] == "on target", s
    turned = world.x - start_x
    assert abs(turned - 540) < 40, turned  # the thing 540 px right of center is now in the middle
    assert world.sticks[0][0] > 0 and world.v == 0.0  # turned right, then let go


def test_track_at_refuses_flat_areas():
    flat = np.full((1080, 1920, 4), 90, np.uint8)
    rt = behave.Runtime(lambda region=None: (flat, time.perf_counter()), PanOutputs(PanWorld()))
    s = wait_state(rt, rt.start("track", {"at": [900, 500]}, max_s=2)["id"])
    assert s["state"] == "failed" and "nothing distinctive" in s["reason"]


def test_press_until_can_hold_a_state_until_the_block_breaks():
    held = {"since": None, "state": None}

    def frame(region=None):
        img = np.full((120, 120, 4), 60, np.uint8)
        if held["since"] and time.perf_counter() - held["since"] > 0.4:
            img[:] = 200  # the block broke: the crosshair area looks completely different
        return img, time.perf_counter()

    class Out:
        def hold(self, state):
            if held["state"] != state:
                held.update(since=time.perf_counter(), state=state)

        def release(self, used=()):
            held.update(since=None, state="released")

    rt = behave.Runtime(frame, Out())
    s = wait_state(rt, rt.start("press_until", {"do": {"hold": {"right_trigger": 1.0}}, "every_ms": 50,
                                                "until": {"change": 30, "region": [0, 0, 120, 120]}}, max_s=5)["id"])
    assert s["state"] == "done" and held["state"] == "released", s


class ProgramOutputs(PanOutputs):
    """Records the full controller states a program holds (and sequences), on top of the panning camera."""

    def __init__(self, w):
        super().__init__(w)
        self.states, self.seqs, self.released = [], [], 0

    def hold(self, state):
        self.states.append({k: (list(v) if isinstance(v, list) else v) for k, v in state.items()})
        rs = state.get("right_stick") or [0, 0]
        self.w.stick("right_stick", rs[0], rs[1])

    def seq(self, steps):
        self.seqs.append(steps)

    def release(self, used=()):
        self.released += 1
        super().release(used)


def run_program(code, max_s=5, world=None, **kw):
    world = world or PanWorld()
    out = ProgramOutputs(world)
    rt = behave.Runtime(world.frame, out, **kw)
    return behave.run_tool(rt, "start", "program", {"code": code, "wait": True}, max_s=max_s), out, world


def test_a_program_holds_whole_controller_states_and_returns_a_result():
    status, out, _ = run_program(
        "pad(ls=(0, 1), buttons=['left_thumb'])\n"          # sprint forward...
        "press('a', ms=30)\n"                                 # ...jump without letting go
        "seq([{'buttons': ['x'], 'ms': 20}])\n"
        "wait(30)\n"
        "log('walked', steps=3)\n"
        "result = {'ok': True}\n")
    assert status["state"] == "done" and status["result"] == {"ok": True}, status
    held = out.states[0]
    assert held["left_stick"] == [0.0, 1.0] and held["buttons"] == ["left_thumb"]
    jump = next(s for s in out.states if "a" in s["buttons"])
    assert jump["left_stick"] == [0.0, 1.0] and "left_thumb" in jump["buttons"]  # the tap rides on the held state
    assert out.states[-1]["buttons"] == ["left_thumb"] and out.seqs == [[{"buttons": ["x"], "ms": 20}]]
    assert any(e["event"] == "log" and e["msg"] == "walked" for e in status["events"]) and out.released == 1


def test_until_returns_the_value_or_none_and_guards_stop_the_program():
    status, _, _ = run_program("t0 = elapsed()\nv = until(lambda: elapsed() - t0 > 0.1 and 'late', timeout=1)\n"
                               "none = until(lambda: False, timeout=0.05)\nresult = [v, none]")
    assert status["result"] == ["late", None], status
    status, out, _ = run_program("guard(lambda: elapsed() > 0.15, 'hurt')\npad(rt=1)\nwait(3000)\nresult = 'never'")
    assert status["state"] == "stopped" and status["reason"] == "guard: hurt" and "result" not in status
    assert out.released == 1 and status["seconds"] < 1.5


def test_programs_end_at_their_limit_even_in_a_loop_that_never_waits():
    status, _, _ = run_program("while True:\n    wait(10)", max_s=0.5)
    assert status["state"] == "stopped" and "time limit" in status["reason"]
    status, _, _ = run_program("x = 0\nwhile True:\n    x += 1", max_s=0.5)
    assert status["state"] == "stopped" and "interrupted" in status["reason"], status
    assert status["seconds"] < 3


def test_program_errors_name_the_line():
    status, _, _ = run_program("wait(1)\nx = 1 / 0\n")
    assert status["state"] == "failed" and "ZeroDivisionError" in status["reason"] and "line 2" in status["reason"]
    status, _, _ = run_program("if True\n  pass")
    assert status["state"] == "failed" and "line 1" in status["reason"]


def test_unknown_names_fail_before_anything_moves():
    status, out, _ = run_program("pad(ls=(0, 1))\nwiat(500)\n")
    assert status["state"] == "failed" and "line 2" in status["reason"] and "'wiat'" in status["reason"], status
    assert "did you mean 'wait'" in status["reason"] and out.states == []  # the stick never moved
    status, _, _ = run_program("def go(n):\n    for i in range(n):\n        wait(1)\n    return n\n"
                               "total = sum(go(k) for k in [1, 2])\nresult = [total, (y := 3), math.pi > 3]")
    assert status["state"] == "done" and status["result"] == [3, 3, True], status  # locals, walrus, API: all known


def test_a_program_aims_while_it_walks():
    world = PanWorld()
    start = world.x
    status, out, _ = run_program("pad(ls=(0, 1))\nr = aim(1500, 540, timeout=6)\nresult = r", max_s=8, world=world)
    assert status["state"] == "done" and status["result"]["on_target"], status
    assert abs((world.x - start) - 540) < 40
    steering = [s for s in out.states if s["right_stick"][0] > 0]
    assert steering and all(s["left_stick"] == [0.0, 1.0] for s in steering)  # kept walking the whole time
    assert out.states[-1]["right_stick"] == [0.0, 0.0] and out.states[-1]["left_stick"] == [0.0, 1.0]


def test_start_and_end_hooks_wrap_every_run():
    calls = []
    status, _, _ = run_program("wait(20)", on_start=lambda run: calls.append("start"),
                               on_end=lambda run: calls.append("end"))
    assert status["state"] == "done" and calls == ["start", "end"]


def test_shift_measures_how_far_the_view_moved():
    frame = _texture(540, 960, block=16, seed=3)
    moved = np.roll(frame, -120, axis=1)  # the camera turned right: the picture moved 120 px left
    dx, dy, peak = behave.phase_shift(frame, moved)
    assert abs(dx + 120) <= 4 and abs(dy) <= 4 and peak > 0.2, (dx, dy, peak)
    assert behave.phase_shift(frame, frame)[:2] == (0, 0)


def test_a_program_can_follow_a_point_with_its_own_tracker():
    status, _, _ = run_program("t = track(1500, 540)\nx, y, score = t.find()\nresult = [round(x), round(y), score > 0.9]")
    assert status["state"] == "done", status
    x, y, confident = status["result"]
    assert abs(x - 1500) <= 8 and abs(y - 540) <= 8 and confident


def test_flat_sky_never_outscores_the_real_match():
    """Aiming at a tree in Minecraft steered into the sky: bright, nearly flat sky made float32 running sums lose a
    window's tiny variance, and those windows scored in the thousands."""
    rng = np.random.default_rng(2)
    img = (250 + rng.normal(0, 0.3, (270, 480))).astype(np.float32)
    img[20:80, 20:80] = rng.integers(0, 255, (60, 60)).astype(np.float32)
    x, y, score = behave.match_template(img, img[30:60, 30:60].copy())
    assert (x, y) == (30, 30) and 0.99 < score <= 1.0, (x, y, score)


class DeadzoneWorld(PanWorld):
    """Like a game with a big stick deadzone: deflections under 0.45 don't turn the camera at all."""

    def stick(self, side, x, y):
        super().stick(side, x if abs(x) >= 0.45 else 0.0, y)


def test_aim_learns_a_deadzone_it_was_not_told_about():
    world = DeadzoneWorld()
    start = world.x
    status, _, _ = run_program("r = aim(1500, 540, timeout=8)\nresult = r", max_s=10, world=world)
    assert status["state"] == "done" and status["result"]["on_target"], status
    assert abs((world.x - start) - 540) < 40 and status["result"]["min_deflection"] >= 0.45


class HandWorld(PanWorld):
    """A held item drawn over the bottom right of every frame, like Minecraft's pickaxe: it never moves."""

    def frame(self, region=None):
        img, t = super().frame(region)
        img[800:1000, 1300:1500] = _texture(200, 200, block=10, seed=9)
        return img, t


def test_aim_says_so_when_the_target_is_part_of_the_screen_overlay():
    status, _, world = run_program("r = aim(1400, 900, timeout=6)\nresult = r", max_s=8, world=HandWorld())
    assert status["state"] == "failed" and "doesn't move when the camera turns" in status["reason"], status
    assert status["seconds"] < 3


class GuardWorld(PanWorld):
    """The panning world with a 'health bar' that tests can drain."""

    def __init__(self):
        super().__init__()
        self.health = 1.0

    def frame(self, region=None):
        img, t = super().frame(None)
        img = img.copy()
        img[1000:1040, 600:1000] = (40, 40, 40, 255)
        img[1000:1040, 600:600 + int(400 * self.health)] = (30, 30, 200, 255)  # BGRA red
        if region:
            img = img[region[1]:region[3], region[0]:region[2]]
        return img, t


def _guard_runtime(world, active=lambda: True, skills=None):
    out = ProgramOutputs(world)
    return behave.Runtime(world.frame, out, active=active, skills=skills, app=lambda: "Game.exe"), out


def test_a_standing_guard_stops_programs_and_starts_a_reflex():
    world = GuardWorld()
    rt, out = _guard_runtime(world)
    guard = rt.start("guard", {"name": "hurt", "setup": "BAR = [600, 1000, 1000, 1040]\nRED0 = color([200, 30, 30], BAR)",
                               "when": "color([200, 30, 30], BAR) < 0.5 * RED0",
                               "program": "pad(ls=(0, -1))\nwait(100)\nresult = 'backed off'"}, max_s=30)
    walker = rt.start("program", {"code": "pad(ls=(0, 1))\nwait(10000)"}, max_s=20)
    time.sleep(0.3)
    world.health = 0.3
    g = wait_state(rt, guard["id"], 5)
    assert g["state"] == "done" and g["reason"] == "fired: hurt", g
    assert rt.status(walker["id"])["state"] == "stopped"
    alerts = rt.take_alerts()
    assert alerts and alerts[0]["guard"] == "hurt" and alerts[0]["stopped"] == [walker["id"]], alerts
    reflex = wait_state(rt, alerts[0]["reflex"], 5)
    assert reflex["state"] == "done" and reflex["result"] == "backed off"
    assert any(st["left_stick"] == [0.0, -1.0] for st in out.states)  # the reflex backed away
    assert rt.take_alerts() is None  # each alert is handed out once


def test_guards_only_judge_a_running_game_and_have_no_controller():
    world, running = GuardWorld(), [False]
    rt, _ = _guard_runtime(world, active=lambda: running[0])
    g = rt.start("guard", {"when": "color([200, 30, 30], [600, 1000, 1000, 1040]) < 0.2", "then": "note"}, max_s=30)
    world.health = 0.0
    time.sleep(0.4)
    assert rt.status(g["id"])["state"] == "running" and rt.take_alerts() is None  # paused: nothing judged
    running[0] = True
    assert wait_state(rt, g["id"], 5)["reason"].startswith("fired")
    bad = rt.start("guard", {"when": "True", "setup": "pad(ls=(0, 1))"}, max_s=5)
    st = wait_state(rt, bad["id"], 5)
    assert st["state"] == "failed" and "unknown name 'pad'" in st["reason"], st
    assert rt.start("guard", {"when": "False"}, max_s=9999)["max_s"] == 9999  # guards may watch for hours


def test_skills_are_saved_found_run_by_name_and_keep_score(tmp_path):
    from relaymcp.device import skills
    world = GuardWorld()
    store = skills.SkillStore(tmp_path)
    rt, out = _guard_runtime(world, skills=store)
    saved = behave.run_tool(rt, "save", "walk_for", {"code": "pad(ls=(0, SPEED))\nwait(MS)\nresult = MS",
                                                      "description": "walk forward at SPEED for MS milliseconds"})
    assert saved["inputs"] == ["SPEED", "MS"] and saved["app"] == "Game.exe", saved
    listed = behave.run_tool(rt, "skills")["skills"]
    assert listed[0]["name"] == "walk_for" and listed[0]["inputs"] == ["SPEED", "MS"]
    st = behave.run_tool(rt, "start", "program", {"skill": "walk_for", "args": {"SPEED": 0.5, "MS": 30},
                                                   "wait": True}, max_s=5)
    assert st["state"] == "done" and st["result"] == 30 and st["skill"] == "walk_for", st
    assert any(s["left_stick"] == [0.0, 0.5] for s in out.states)
    st = behave.run_tool(rt, "start", "program", {"code": "r = skill('walk_for', SPEED=1, MS=20)\nresult = r * 2",
                                                   "wait": True}, max_s=5)
    assert st["state"] == "done" and st["result"] == 40, st
    with pytest.raises(ValueError, match="needs MS"):
        rt.runs.clear()
        behave.run_tool(rt, "start", "program", {"skill": "walk_for", "args": {"SPEED": 1}, "wait": True}, max_s=5)
    entry = behave.run_tool(rt, "skills")["skills"][0]
    assert entry["runs"] == 2 and entry["ok"] == 2 and entry["last"] == "done", entry
    assert behave.run_tool(rt, "forget", "walk_for")["forgot"] and behave.run_tool(rt, "skills")["skills"] == []
