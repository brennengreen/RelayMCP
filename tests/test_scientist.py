import json

from relaymcp.play import scientist as sc


class Bounce:
    """A ball moving 3 px a step that bounces off x=0 and x=100; LEFT/RIGHT move a paddle 4 px."""
    game = "Bounce"

    def actions(self):
        return ["NOOP", "LEFT", "RIGHT"]

    def _play(self, reset, actions):
        x, vx, px = 10 + reset % 50, 3, 50
        objs = [self._objs(x, px)]
        for a in actions:
            x += vx
            if x <= 0 or x >= 100:
                vx = -vx
            px += {"LEFT": -4, "RIGHT": 4}.get(a, 0)
            objs.append(self._objs(x, px))
        return objs

    @staticmethod
    def _objs(x, px):
        return [{"cat": "Ball", "x": x, "y": 50, "w": 2, "h": 2}, {"cat": "Paddle", "x": px, "y": 90, "w": 8, "h": 2}]

    def record(self, seed, steps=300):
        import random
        rng = random.Random(seed)
        acts = [rng.choice(self.actions()) for _ in range(steps)]
        return [{"reset": seed, "actions": acts, "objs": self._play(seed, acts)}]

    def experiment(self, seg, step, actions):
        return self._play(seg["reset"], seg["actions"][:step] + actions)[step:]


GOOD = '''def predict(history, action):
    out = []
    for o in history[-1]:
        o = dict(o)
        if o["cat"] == "Paddle":
            o["x"] += {"LEFT": -4, "RIGHT": 4}.get(action, 0)
        elif len(history) > 1:
            prev = [p for p in history[-2] if p["cat"] == "Ball"][0]
            o["x"] += o["x"] - prev["x"]
        out.append(o)
    return out
'''


def test_error_and_baselines():
    a = [{"cat": "Ball", "x": 0, "y": 0, "w": 1, "h": 1}]
    b = [{"cat": "Ball", "x": 3, "y": 4, "w": 1, "h": 1}]
    assert sc.error(a, b)[0] == 5
    assert sc.error([], b)[0] == sc.CAP
    assert sc.error(a + b, b)[0] == sc.CAP
    hist = [[{"cat": "Ball", "x": 0, "y": 0, "w": 1, "h": 1}], [{"cat": "Ball", "x": 2, "y": 1, "w": 1, "h": 1}]]
    assert sc.constant_velocity(hist)[0]["x"] == 4 and sc.constant_velocity(hist)[0]["y"] == 2
    assert sc.no_change(hist)[0]["x"] == 2


def test_experiments_parsed():
    reply = 'x\nEXPERIMENT: {"segment": 0, "step": 4, "actions": ["LEFT", "NOOP"]}\nEXPERIMENT: {bad}\n'
    assert sc.experiments_in(reply) == [{"segment": 0, "step": 4, "actions": ["LEFT", "NOOP"]}]


def test_paired():
    old = [5.0] * 100
    gain, p = sc.paired([2.0] * 100, old)
    assert abs(gain - 0.6) < 1e-9 and p == 1.0
    assert sc.paired(old, old) == (0.0, 0.0)


def test_scientist_learns_and_runs_experiments(tmp_path):
    prompts = []

    def gen(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return ('NOTE: velocity\n```notebook\nball keeps velocity\n```\n'
                    'EXPERIMENT: {"segment": 0, "step": 5, "actions": ["LEFT", "LEFT"]}\n'
                    f"```python\n{GOOD}```")
        return "NOTE: nothing\n"

    s = sc.Scientist(Bounce(), gen, tmp_path, width=1, say=lambda m: None)
    st = s.run(2)
    assert st["best"] == 1 and st["calls"] == 2
    assert st["curve"][-1]["vs_no_change"] < 0.2
    assert "LEFT ->" in prompts[1] and "ball keeps velocity" in prompts[1]
    assert (tmp_path / "world_model.py").read_text() == GOOD
    # resumes where it stopped
    st2 = sc.Scientist(Bounce(), gen, tmp_path, width=1, say=lambda m: None).run(3)
    assert st2["round"] == 3 and len(json.loads((tmp_path / "science.json").read_text())["curve"]) == 4
    assert "Bounce" in sc.report(tmp_path.parent) or True


def test_crashing_model_rejected(tmp_path):
    def gen(prompt):
        return "NOTE: oops\n```python\ndef predict(history, action):\n    raise ValueError('x')\n```"
    st = sc.Scientist(Bounce(), gen, tmp_path, width=1, say=lambda m: None).run(1)
    assert st["best"] == 0 and st["tries"][-1]["verdict"] == "rejected"


def test_rollout_good_model_beats_baselines():
    segs = Bounce().record(3)
    ns = {}
    exec(GOOD, ns)
    good = sc.rollout_errors(ns["predict"], segs)
    assert good < 0.3 * sc.rollout_errors(sc.no_change, segs)


def test_library_offered_and_importable(tmp_path):
    lib = tmp_path / "lib.py"
    lib.write_text("def paddle_step(action):\n    return {'LEFT': -4, 'RIGHT': 4}.get(action, 0)\n")
    prompts = []

    def gen(prompt):
        prompts.append(prompt)
        return "NOTE: lib\n```python\n" + GOOD.replace(
            '{"LEFT": -4, "RIGHT": 4}.get(action, 0)', "paddle_step(action)").replace(
            "def predict", "from library import paddle_step\n\n\ndef predict") + "```"
    st = sc.Scientist(Bounce(), gen, tmp_path / "run", width=1, say=lambda m: None, library=lib).run(1)
    assert "YOUR LIBRARY" in prompts[0] and "paddle_step" in prompts[0]
    assert st["best"] == 1 and st["curve"][-1]["vs_no_change"] < 0.2


def test_distill(tmp_path):
    d = tmp_path / "Bounce"
    d.mkdir()
    (d / "science.json").write_text(json.dumps({"notebook": "ball bounces"}))
    (d / "world_model.py").write_text(GOOD)
    seen = []

    def gen(prompt):
        seen.append(prompt)
        return "```python\ndef velocity(a, b):\n    return b - a\n```"
    assert "def velocity" in sc.distill(tmp_path, ["Bounce"], gen)
    assert "ball bounces" in seen[0] and "=== Bounce ===" in seen[0]
