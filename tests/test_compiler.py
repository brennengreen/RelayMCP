from types import SimpleNamespace

import numpy as np
import pytest

from relaymcp.play import compiler as c


def box(size=100):
    pts = [(0, 0), (size, 0), (size, size), (0, size)]
    lines = [SimpleNamespace(x1=a[0], y1=a[1], x2=b[0], y2=b[1], is_blocking=True)
             for a, b in zip(pts, pts[1:] + pts[:1])]
    return [SimpleNamespace(lines=lines)]


def state(**kw):
    st = {"x": 25.0, "y": 50.0, "angle": 0.0, "health": 100, "ammo": 50, "monsters": [], "items": [],
          "layout": box()}
    st.update(kw)
    return st


def test_rays_measure_walls_around_the_facing():
    r = c.rays(state(), n=4, far=512.0)
    assert r[0] == pytest.approx(75 / 512)  # ahead (east)
    assert r[1] == pytest.approx(50 / 512)  # left (north)
    assert r[2] == pytest.approx(25 / 512)  # behind


def test_senses_and_contested(monkeypatch):
    monkeypatch.setattr(c, "NAMES", ["DoomImp", "Zombieman"])
    s = c.Senses()
    st = state(monsters=[{"name": "DoomImp", "distance": 500, "bearing": 90, "visible": True}])
    s.see(st)
    x = s(st)
    assert x.shape == (4 + c.SLOTS * 6 + 4 * len(c.ITEM_KINDS) + c.RAYS,)
    assert x[4] == 1.0 and x[5] == pytest.approx(0.5) and x[8] == 1.0  # present, distance, an imp
    assert c.contested(st, s)
    calm = state()
    s.see(calm)
    assert not c.contested(calm, s)
    s.see(state(health=80))
    assert c.contested(state(health=80), s)  # hurt lately


def test_value_rewards_the_tally_and_punishes_death():
    before = {"kills": 0, "dealt": 0.0, "taken": 0.0, "died": False}
    win = c.value(before, {"kills": 1, "dealt": 60.0, "taken": 0.0, "died": False}, 2, False)
    dead = c.value(before, {"kills": 1, "dealt": 60.0, "taken": 100.0, "died": True}, 2, False)
    assert win > 1 > dead


class Base:
    def act(self, st, tic):
        return [1, 0, 1, 0, 0, 0, 5.0, 0]


def test_compiled_overrides_only_movement():
    agent = c.Compiled(Base())
    st = state(monsters=[{"name": "DoomImp", "distance": 300, "bearing": 0}])
    assert agent.act(st, 0) == [1, 0, 1, 0, 0, 0, 5.0, 0]  # no student: the base's own moves
    agent.force = (c.OPTIONS.index("back-left"), c.HOLD)
    a = agent.act(st, 1)
    assert a[0] == 1 and a[6] == 5.0 and a[2:6] == [0, 1, 1, 0] and a[1] == 1
    assert agent.act(st, c.HOLD)[2:6] == [0, 1, 1, 0]  # held
    assert agent.act(st, c.HOLD + 1)[2:6] == [1, 0, 0, 0]  # then back to the base


def test_train_and_net_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "MARGIN", 0.0)
    rng = np.random.default_rng(0)
    X = rng.normal(size=(3000, 6)).astype(np.float32)
    V = np.zeros((3000, len(c.OPTIONS)), np.float32)
    V[np.arange(3000), np.where(X[:, 0] > 0, 2, 4)] = 1.0  # back when x0 > 0, else right
    np.savez(tmp_path / "r1.npz", X=X, V=V, chosen=V.argmax(1))
    fit = c.train([tmp_path / "r1.npz"], tmp_path / "s.json", hidden=16, epochs=80)
    assert fit["agree"] > 0.9 and fit["regret"] < fit["base_regret"]
    net = c.Net.load(tmp_path / "s.json")
    assert int(np.argmax(net.logits(np.array([2, 0, 0, 0, 0, 0], np.float32)))) == 2
    assert int(np.argmax(net.logits(np.array([-2, 0, 0, 0, 0, 0], np.float32)))) == 4


def test_margin_keeps_the_base_unless_clearly_better(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "MARGIN", 0.5)
    X = np.random.default_rng(1).normal(size=(2000, 4)).astype(np.float32)
    V = np.zeros((2000, len(c.OPTIONS)), np.float32)
    V[:, 3] = 0.2  # a small, unconvincing edge
    np.savez(tmp_path / "r1.npz", X=X, V=V, chosen=V.argmax(1))
    c.train([tmp_path / "r1.npz"], tmp_path / "s.json", hidden=8, epochs=60)
    net = c.Net.load(tmp_path / "s.json")
    assert int(np.argmax(net.logits(X[0]))) == 0


def test_a_load_brings_back_a_game_that_ended(tmp_path):
    pytest.importorskip("vizdoom")
    g = c.Game("MAP05", 1, 2)
    for _ in range(20):
        g.d.g.make_action([0] * 8)
        g.tic += 1
    sav = str(tmp_path / "s.sav")
    g.d.g.save(sav)
    g.d.g.send_game_command("kill")
    for _ in range(5):
        g.d.g.make_action([0] * 8)
    assert g.done()
    g.load(sav, 20)
    assert not g.done() and not g.d.g.is_player_dead() and g.state() is not None
    while not g.done():
        g.d.g.make_action([0] * 8)
        g.tic += 1
    assert g.tic == 70 and not g.exited()  # the limit is the tics played, not ViZDoom's clock
    g.d.close()
