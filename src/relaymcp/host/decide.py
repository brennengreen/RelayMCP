"""Fast bounded decisions: pick one answer from a fixed list in milliseconds, for the tactics between the handheld's
real-time loops (1-35 ms, deterministic) and the planning model (seconds per step).

A decision is f(state, question, answers): nothing is written. The instructions, the intent, the question and its
lettered answers come first and are run through the model once; their attention state is kept. Each call then feeds
only the new state and reads the next-token scores of the answer letters from one forward pass, renormalized over
those letters. That is a way of running an existing small model, not a new model (the idea behind decision APIs such
as OpenAI's and TypeSafe's Jev), so it can run locally, next to the game, with no network in the loop.

Backends share one call: `Rules` (a function: instant and exact) and `MLXDecider` (a local model on Apple Silicon,
imported only when used). A remote decision API can sit behind the same call later."""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

LETTERS = "ABCDEFGHIJKLMNOP"
STATE_MARK = "\u2063STATE\u2063"  # where the state goes in the rendered prompt (invisible separators: never in a state)

SYSTEM = ("You make fast decisions for an agent playing a video game. Read the intent, the question, the options and "
          "the state, then answer with the letter of the one option the intent calls for. Answer with the letter only.")


@dataclass
class Decision:
    choice: str
    probs: dict = field(default_factory=dict)  # answer -> probability (renormalized over the answers)
    ms: float = 0.0
    backend: str = ""
    fallback: bool = False  # the default answer (a timeout or a failure), not the backend's own
    prepare_ms: float = 0.0  # a question's first use runs its fixed prompt once

    @property
    def confidence(self) -> float:
        return self.probs.get(self.choice, 0.0)


@dataclass(frozen=True)
class Question:
    """A question with a fixed answer list: the part of a decision that stays the same from call to call."""
    text: str
    answers: tuple  # ((name, description), ...)
    intent: str = ""
    examples: tuple = ()  # ((state text, answer name), ...): worked examples, cached with the prompt (free per call)

    @staticmethod
    def make(text: str, answers, intent: str = "", examples=()) -> Question:
        items = answers.items() if isinstance(answers, dict) else [(a, "") if isinstance(a, str) else a for a in answers]
        pairs = tuple((str(n), str(d)) for n, d in items)
        if not 2 <= len(pairs) <= len(LETTERS):
            raise ValueError(f"a decision needs 2-{len(LETTERS)} answers")
        if len({n for n, _ in pairs}) != len(pairs):
            raise ValueError("answer names must differ")
        names = [n for n, _ in pairs]
        shots = tuple((render_state(st), str(a)) for st, a in examples)
        if any(a not in names for _, a in shots):
            raise ValueError("an example's answer isn't one of the answers")
        return Question(text.strip(), pairs, intent.strip(), shots)

    @property
    def names(self) -> list:
        return [n for n, _ in self.answers]

    def user_message(self, state_text: str) -> str:
        options = "\n".join(f"{LETTERS[i]}) {n}" + (f": {d}" if d else "") for i, (n, d) in enumerate(self.answers))
        intent = f"Intent:\n{self.intent}\n\n" if self.intent else ""
        names = self.names
        shots = "".join(f"State:\n{st}\nAnswer: {LETTERS[names.index(a)]}\n\n" for st, a in self.examples)
        shots = f"Examples:\n\n{shots}" if shots else ""
        return f"{intent}Question: {self.text}\nOptions:\n{options}\n\n{shots}State:\n{state_text}"


def render_state(state: Any) -> str:
    if isinstance(state, str):
        return state
    return json.dumps(state, separators=(",", ":"), ensure_ascii=False, default=str)


class Rules:
    """A decision as code: instant, exact, and the reference a model is measured against."""

    def __init__(self, fn: Callable[[Any], str], name: str = "rules"):
        self.fn, self.name = fn, name

    def decide(self, q: Question, state: Any) -> Decision:
        t0 = time.perf_counter()
        choice = self.fn(state)
        if choice not in q.names:
            raise ValueError(f"rules answered {choice!r}, not one of {q.names}")
        return Decision(choice, {n: float(n == choice) for n in q.names}, (time.perf_counter() - t0) * 1000, self.name)


class MLXDecider:
    """A local model as a decision function (Apple Silicon, mlx-lm). Per question, the prompt up to the state is run
    once and its cache kept; each decision runs only the state and the end of the prompt, then restores the cache
    (attention caches are trimmed back; recurrent states, as in hybrid models, are put back)."""

    def __init__(self, model: str, calibrate: bool = False, system: str = SYSTEM):
        from mlx_lm import load
        self.model_id = model
        self.model, self.tok = load(model)
        self.system, self.calibrate = system, calibrate
        self._prepared: dict = {}
        self._lock = threading.Lock()  # one decision at a time: they share the cache

    # -- prompt --------------------------------------------------------------------------------------------------------

    def _render(self, q: Question) -> tuple[str, str]:
        messages = [{"role": "system", "content": self.system}, {"role": "user", "content": q.user_message(STATE_MARK)}]
        try:
            text = self.tok.apply_chat_template(messages, add_generation_prompt=True, tokenize=False,
                                                enable_thinking=False)
        except Exception:  # a template without a system role: fold it into the user message
            messages = [{"role": "user", "content": self.system + "\n\n" + q.user_message(STATE_MARK)}]
            text = self.tok.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
        before, _, after = text.partition(STATE_MARK)
        return before, after

    def _encode(self, text: str) -> list:
        return list(self.tok.encode(text, add_special_tokens=False))

    def _label_ids(self, n: int) -> list:
        """Each answer letter's token ids (bare and with a leading space: tokenizers differ at the reply's start)."""
        out = []
        for letter in LETTERS[:n]:
            ids = set()
            for form in (letter, " " + letter):
                t = self._encode(form)
                if len(t) == 1:
                    ids.add(t[0])
            if not ids:
                raise RuntimeError(f"no single token for answer letter {letter!r} in {self.model_id}")
            out.append(sorted(ids))
        return out

    def _prepare(self, q: Question) -> dict:
        p = self._prepared.get(q)
        if p is not None:
            return p
        import mlx.core as mx
        from mlx_lm.models.cache import make_prompt_cache
        before, after = self._render(q)
        prefix = self._encode(before)
        cache = make_prompt_cache(self.model)
        self.model(mx.array([prefix]), cache=cache)
        mx.eval([c.state for c in cache])
        p = {"cache": cache, "saved": _save(cache), "after": after, "labels": self._label_ids(len(q.answers)),
             "prefix_tokens": len(prefix), "prior": None}
        self._prepared[q] = p
        if self.calibrate:  # the letters' bias with an empty state (contextual calibration)
            p["prior"] = self._scores(q, p, "(no state)")
        return p

    def _scores(self, q: Question, p: dict, state_text: str) -> list:
        import mlx.core as mx
        tokens = self._encode(state_text + p["after"])
        try:
            logits = self.model(mx.array([tokens]), cache=p["cache"])[0, -1].astype(mx.float32)
            per_label = mx.stack([mx.max(logits[mx.array(ids)]) for ids in p["labels"]])
            probs = mx.softmax(per_label)
            mx.eval(probs)
            return probs.tolist()
        finally:
            _restore(p["cache"], p["saved"], len(tokens))

    # -- deciding ------------------------------------------------------------------------------------------------------

    def decide(self, q: Question, state: Any) -> Decision:
        with self._lock:
            t0 = time.perf_counter()
            p = self._prepare(q)
            t1 = time.perf_counter()
            probs = self._scores(q, p, render_state(state))
            if p["prior"]:
                probs = [x / max(pr, 1e-6) for x, pr in zip(probs, p["prior"])]
                s = sum(probs)
                probs = [x / s for x in probs]
            ms = (time.perf_counter() - t1) * 1000  # the decision itself; a question's first use also prepares it
            dist = {n: float(x) for n, x in zip(q.names, probs)}
            choice = max(dist, key=dist.get)
            return Decision(choice, dist, ms, f"mlx:{self.model_id.split('/')[-1]}", prepare_ms=(t1 - t0) * 1000)


def _save(cache) -> list:
    """What to put back after a decision: nothing for caches that trim; the state of those that can't (recurrent)."""
    out = []
    for c in cache:
        if c.is_trimmable():
            out.append(None)
        elif isinstance(getattr(c, "cache", None), list):
            out.append(("arrays", list(c.cache), c.left_padding, c.lengths))
        elif hasattr(c, "caches"):
            out.append(("list", _save(c.caches)))
        else:
            raise RuntimeError(f"can't reuse a {type(c).__name__} prompt cache")
    return out


def _restore(cache, saved, n_tokens: int) -> None:
    for c, s in zip(cache, saved):
        if s is None:
            c.trim(n_tokens)
        elif s[0] == "arrays":
            c.cache, c.left_padding, c.lengths = list(s[1]), s[2], s[3]
        else:
            _restore(c.caches, s[1], n_tokens)


# --- intents compiled to code ----------------------------------------------------------------------------------------

POLICY_PROMPT = """Write a Python function `decide(state)` that applies this intent and returns one of the answer names.

Intent:
{intent}

Question: {question}
Answers (return exactly one of these strings): {answers}

`state` is a dict like this example:
{example}
{schema}
Rules for the code: plain Python only, no imports, no I/O, no classes, no names starting with an underscore. Use only
the state. Follow the intent's order of priority exactly. Reply with only the code in one ```python block."""

SAFE_BUILTINS = {n: getattr(__import__("builtins"), n) for n in (
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "isinstance", "len", "list", "map",
    "max", "min", "range", "reversed", "round", "set", "sorted", "str", "sum", "tuple", "zip", "True", "False", "None")}


class Policy(Rules):
    """A decision compiled from an intent into code (by a model, once): microseconds per decision and exact, until the
    intent changes and it is compiled again."""

    def __init__(self, fn: Callable[[Any], str], source: str, name: str = "policy", compile_ms: float = 0.0):
        super().__init__(fn, name)
        self.source, self.compile_ms = source, compile_ms


def check_policy_source(source: str) -> None:
    """Refuse code that could reach outside the state: imports, private or dunder names, attribute access to anything
    underscored, and calls to builtins not on the list (exec happens with only SAFE_BUILTINS anyway)."""
    import ast
    tree = ast.parse(source)
    defs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.ClassDef, ast.AsyncFunctionDef,
                             ast.Await, ast.Yield, ast.YieldFrom, ast.With, ast.AsyncWith, ast.Try, ast.Raise,
                             ast.Delete)):
            raise ValueError(f"policy code may not use {type(node).__name__}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError(f"policy code may not touch .{node.attr}")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise ValueError(f"policy code may not use {node.id}")
    if "decide" not in defs:
        raise ValueError("policy code must define decide(state)")
    defined = set(defs)
    for node in ast.walk(tree):  # every name it binds: arguments, assignments, loop and comprehension targets
        if isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
    for node in ast.walk(tree):  # and nothing else but the safe builtins (no open, eval, getattr, ...)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in defined \
                and node.id not in SAFE_BUILTINS:
            raise ValueError(f"policy code may not use {node.id}")


def load_policy(source: str) -> Callable[[Any], str]:
    check_policy_source(source)
    ns: dict = {"__builtins__": dict(SAFE_BUILTINS)}
    exec(compile(source, "<policy>", "exec"), ns)  # noqa: S102 (checked above; only safe builtins)
    return ns["decide"]


def extract_code(text: str) -> str:
    import re
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip() + "\n"


def compile_policy(q: Question, samples: Sequence[Any], generate: Callable[[str], str], schema: str = "",
                   attempts: int = 2) -> Policy:
    """Have a model write the intent as code, then check it: it must load, and answer every sample state with one of
    the answers (and match any worked examples). A failed check is fed back once."""
    prompt = POLICY_PROMPT.format(intent=q.intent, question=q.text, answers=", ".join(q.names),
                                  example=render_state(samples[0]), schema=schema)
    t0 = time.perf_counter()
    problem = ""
    for _ in range(attempts):
        source = extract_code(generate(prompt + problem))
        try:
            fn = load_policy(source)
            for st in samples:
                a = fn(st)
                if a not in q.names:
                    raise ValueError(f"returned {a!r} for {render_state(st)}")
            for st, want in q.examples:
                if fn(json.loads(st)) != want:
                    raise ValueError(f"answered {fn(json.loads(st))!r}, not {want!r}, for {st}")
        except Exception as e:  # noqa: BLE001
            problem = f"\n\nYour previous code failed a check: {type(e).__name__}: {e}. Fix it."
            continue
        return Policy(fn, source, compile_ms=(time.perf_counter() - t0) * 1000)
    raise RuntimeError(f"couldn't compile the intent into a working policy{problem.strip() and ': ' + problem.strip()}")


def ollama_generate(model: str, url: str = "http://127.0.0.1:11434") -> Callable[[str], str]:
    """A local generator for compile_policy (Ollama, no thinking, deterministic)."""
    import urllib.request

    def generate(prompt: str) -> str:
        body = json.dumps({"model": model, "prompt": prompt, "stream": False, "think": False,
                           "options": {"temperature": 0}}).encode()
        req = urllib.request.Request(f"{url}/api/generate", body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())["response"]
    return generate


def openai_generate(model: str, base_url: str | None = None, key: str | None = None) -> Callable[[str], str]:
    """Any OpenAI-compatible chat endpoint (OpenAI, OpenRouter, Together, vLLM, LM Studio...): OPENAI_BASE_URL and
    OPENAI_API_KEY by default."""
    import os
    import urllib.request
    url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    key = key or os.environ.get("OPENAI_API_KEY", "")

    def generate(prompt: str) -> str:
        body = json.dumps({"model": model, "temperature": 0,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request(url, body, {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]
    return generate


def copilot_generate(model: str, system: str = "Answer briefly, exactly in the format asked.",
                     timeout: float = 120) -> Callable[[str], str]:
    """A model through GitHub Copilot (any model your plan has, e.g. claude-opus-5.5): one warm runtime
    (github-copilot-sdk, Python 3.11+) on its own thread, a fresh tool-free session per prompt. After the first prompt
    a short answer takes ~1-2 s (a `copilot -p` run spends ~5 s just starting)."""
    import asyncio
    import shutil
    import tempfile

    from copilot import CopilotClient, RuntimeConnection
    from copilot.session import PermissionHandler
    from copilot.session_events import AssistantMessageData, SessionErrorData, SessionIdleData

    exe = shutil.which("copilot")
    if not exe:
        raise RuntimeError("copilot: the GitHub Copilot CLI isn't on PATH")
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, name="copilot-generate", daemon=True).start()
    workdir = tempfile.mkdtemp(prefix="spinal-copilot-")  # no repository, no custom instructions
    client = CopilotClient(connection=RuntimeConnection.for_stdio(path=exe), log_level="error")
    asyncio.run_coroutine_threadsafe(client.start(), loop).result(60)

    async def ask(prompt: str) -> str:
        session = await client.create_session(
            on_permission_request=PermissionHandler.approve_all, model=model, available_tools=[],
            tool_search={"enabled": False}, skip_custom_instructions=True, working_directory=workdir,
            system_message={"mode": "replace", "content": system})
        reply, error, done = "", None, asyncio.Event()

        def on_event(ev):
            nonlocal reply, error
            if isinstance(ev.data, AssistantMessageData):
                reply = (ev.data.content or "").strip() or reply
            elif isinstance(ev.data, SessionErrorData):
                error = getattr(ev.data, "message", None) or str(ev.data)
                done.set()
            elif isinstance(ev.data, SessionIdleData):
                done.set()
        session.on(on_event)
        try:
            await session.send(prompt)
            await asyncio.wait_for(done.wait(), timeout)
        finally:
            try:
                await session.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if error and not reply:
            raise RuntimeError(error)
        return reply

    def generate(prompt: str) -> str:
        return asyncio.run_coroutine_threadsafe(ask(prompt), loop).result(timeout + 30)
    return generate


def generator(spec: str) -> Callable[[str], str]:
    """A model by name: "ollama:qwen3.5:9b", "openai:gpt-5.4-mini" (any OpenAI-compatible API), "copilot:claude-opus-5.5"
    (GitHub Copilot), or a bare Ollama name."""
    kind, _, name = spec.partition(":")
    if kind == "openai":
        return openai_generate(name)
    if kind == "copilot":
        return copilot_generate(name)
    return ollama_generate(name if kind == "ollama" else spec)


class Deadline:
    """A decider with a time limit: it answers `default` (marked fallback) when the backend is late or fails, so a
    caller in a control loop never waits on it. Decisions run on one worker thread that owns the backend."""

    def __init__(self, make: Callable[[], Any], timeout_ms: float):
        import queue
        self.timeout_s = timeout_ms / 1000.0
        self._jobs: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        threading.Thread(target=self._work, args=(make,), name="decider", daemon=True).start()
        self._ready.wait()
        if self._error:
            raise self._error

    def _work(self, make) -> None:
        try:
            self.backend = make()
        except BaseException as e:  # noqa: BLE001
            self._error = e
            self._ready.set()
            return
        self._ready.set()
        while True:
            q, state, box, done = self._jobs.get()
            try:
                box.append(self.backend.decide(q, state))
            except Exception as e:  # noqa: BLE001
                box.append(e)
            done.set()

    def decide(self, q: Question, state: Any, default: str) -> Decision:
        t0 = time.perf_counter()
        box: list = []
        done = threading.Event()
        self._jobs.put((q, state, box, done))
        if done.wait(self.timeout_s) and box and isinstance(box[0], Decision):
            return box[0]
        return Decision(default, {n: float(n == default) for n in q.names}, (time.perf_counter() - t0) * 1000,
                        "fallback", fallback=True)


def agreement(decisions: Sequence[Decision], truth: Sequence[str]) -> float:
    return sum(d.choice == t for d, t in zip(decisions, truth)) / max(1, len(truth))


def percentile(values: Sequence[float], p: float) -> float:
    v = sorted(values)
    return v[min(len(v) - 1, max(0, math.ceil(p / 100 * len(v)) - 1))] if v else float("nan")
