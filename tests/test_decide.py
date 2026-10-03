"""The decision engine without a model: questions, rules, intents compiled to checked code, deadlines and the prompt
cache's save/restore (the MLX backend itself is measured in .scratch, not in CI)."""

import time

import pytest

from relaymcp.host import decide

Q = decide.Question.make("What now?", {"keep_working": "carry on", "fight": "attack", "retreat": "back off"},
                         "Retreat from a creeper within 6 blocks; fight anything else within 4; else keep working.")


def oracle(s):
    if s["creeper"] is not None and s["creeper"] <= 6:
        return "retreat"
    if s["hostile"] is not None and s["hostile"] <= 4:
        return "fight"
    return "keep_working"


SAMPLES = [{"creeper": c, "hostile": h} for c in (None, 3, 9) for h in (None, 2, 7)]


def test_questions_letter_their_answers_and_keep_examples_in_the_fixed_part():
    q = decide.Question.make("Go?", ["yes", "no"], "Go when clear.", examples=[({"clear": True}, "yes")])
    text = q.user_message("STATE")
    assert "A) yes" in text and "B) no" in text and text.endswith("State:\nSTATE")
    assert 'State:\n{"clear":true}\nAnswer: A' in text  # worked examples sit before the state (cached once)
    with pytest.raises(ValueError):
        decide.Question.make("x", ["only"])
    with pytest.raises(ValueError):
        decide.Question.make("x", ["a", "a"])
    with pytest.raises(ValueError):
        decide.Question.make("x", ["a", "b"], examples=[({}, "c")])


def test_rules_answer_instantly_and_only_with_an_answer():
    d = decide.Rules(oracle).decide(Q, {"creeper": 5, "hostile": None})
    assert d.choice == "retreat" and d.probs == {"keep_working": 0.0, "fight": 0.0, "retreat": 1.0} and d.ms < 5
    with pytest.raises(ValueError):
        decide.Rules(lambda s: "dance").decide(Q, {})


GOOD = '''```python
def decide(state):
    c, h = state["creeper"], state["hostile"]
    if c is not None and c <= 6:
        return "retreat"
    if h is not None and h <= 4:
        return "fight"
    return "keep_working"
```'''


def test_an_intent_compiles_to_a_checked_policy_that_decides_in_microseconds():
    prompts = []
    pol = decide.compile_policy(Q, SAMPLES, lambda p: prompts.append(p) or GOOD)
    assert "Retreat from a creeper" in prompts[0] and "keep_working, fight, retreat" in prompts[0]
    assert all(pol.decide(Q, s).choice == oracle(s) for s in SAMPLES)
    t = time.perf_counter()
    for _ in range(1000):
        pol.decide(Q, SAMPLES[4])
    assert (time.perf_counter() - t) / 1000 < 0.001  # well under a millisecond each


@pytest.mark.parametrize("bad", [
    "import os\ndef decide(state):\n    return 'fight'",
    "def decide(state):\n    return state.__class__.__name__",
    "def decide(state):\n    open('/tmp/x', 'w')\n    return 'fight'",
    "def decide(state):\n    return __import__('os').name",
    "def choose(state):\n    return 'fight'",
])
def test_policy_code_that_reaches_outside_the_state_is_refused(bad):
    with pytest.raises(ValueError):
        decide.load_policy(bad)


def test_a_policy_that_fails_its_check_is_sent_back_once_with_the_problem():
    replies = iter(["def decide(state):\n    return 'dance'", GOOD])
    prompts = []
    pol = decide.compile_policy(Q, SAMPLES, lambda p: prompts.append(p) or next(replies))
    assert len(prompts) == 2 and "returned 'dance'" in prompts[1] and pol.decide(Q, SAMPLES[1]).choice == "fight"
    with pytest.raises(RuntimeError, match="couldn't compile"):
        decide.compile_policy(Q, SAMPLES, lambda p: "def decide(state):\n    return 'dance'")


def test_a_late_backend_gives_the_default_and_never_holds_up_the_caller():
    class Slow:
        def decide(self, q, state):
            time.sleep(0.3)
            return decide.Decision("fight", {}, 300.0, "slow")

    d = decide.Deadline(Slow, timeout_ms=40)
    t = time.perf_counter()
    got = d.decide(Q, {}, default="keep_working")
    assert got.fallback and got.choice == "keep_working" and time.perf_counter() - t < 0.2
    fast = decide.Deadline(lambda: decide.Rules(oracle), timeout_ms=200)
    assert fast.decide(Q, {"creeper": 1, "hostile": None}, default="keep_working").choice == "retreat"


class FakeKV:
    def __init__(self):
        self.offset = 10

    def is_trimmable(self):
        return True

    def trim(self, n):
        self.offset -= n


class FakeArrays:
    def __init__(self):
        self.cache, self.left_padding, self.lengths = ["s0", "s1"], None, None

    def is_trimmable(self):
        return False


def test_the_prompt_cache_goes_back_to_the_fixed_prompt_after_each_decision():
    kv, arrays = FakeKV(), FakeArrays()
    saved = decide._save([kv, arrays])
    kv.offset += 7  # a decision's 7 tokens
    arrays.cache[0] = "after the state"  # a recurrent layer's state moved on
    decide._restore([kv, arrays], saved, 7)
    assert kv.offset == 10 and arrays.cache == ["s0", "s1"]
