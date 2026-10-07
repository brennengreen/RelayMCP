from relaymcp.play import planner as pl


def predict(history, action):
    o = dict(history[-1][0])
    o["x"] += {"LEFT": -4, "RIGHT": 4}.get(action, 0)
    return [o]


def test_planner_moves_toward_goal():
    def value(history):
        return -abs(history[-1][0]["x"] - 100)
    p = pl.Planner(predict, value, ["NOOP", "LEFT", "RIGHT"], seed=0)
    hist = [[{"cat": "Paddle", "x": 50, "y": 0, "w": 8, "h": 2}]]
    assert p.act(hist) == "RIGHT"
    p = pl.Planner(predict, value, ["NOOP", "LEFT", "RIGHT"], seed=0)
    hist = [[{"cat": "Paddle", "x": 150, "y": 0, "w": 8, "h": 2}]]
    assert p.act(hist) == "LEFT"
    assert p.act(hist) == "LEFT"  # the plan is kept for REPLAN steps


def test_hns_and_paired():
    assert pl.hns("Breakout", 30.5) == 1.0 and pl.hns("Breakout", 1.7) == 0.0
    assert pl.paired([1, 1, 1], [0, 0, 0])[1] == 1.0
    assert pl.paired([0, 0], [0, 0]) == (0.0, 0.0)
