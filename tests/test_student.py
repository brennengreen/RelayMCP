import numpy as np

from relaymcp.play import student as st


def test_encoder_and_mlp_learn_a_rule():
    enc = st.Encoder({"Ball", "Paddle"})
    X, y = [], []
    rng = np.random.default_rng(0)
    for _ in range(2000):
        bx, px = rng.integers(0, 150, 2)
        h = [[{"cat": "Ball", "x": int(bx), "y": 50, "w": 2, "h": 2}, {"cat": "Paddle", "x": int(px), "y": 90,
                                                                      "w": 8, "h": 2}]]
        X.append(enc(h))
        y.append(2 if bx > px else 1)
    X, y = np.array(X, np.float32), np.array(y)
    net = st.MLP(enc.size, 3).fit(X[:1800], y[:1800], epochs=40)
    assert enc.size == 2 * st.SLOTS * st.FEATS
    assert net.accuracy(X[1800:], y[1800:]) > 0.9
