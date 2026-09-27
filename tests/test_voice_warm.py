"""The warm voice runtime, against a fake github-copilot-sdk (no network, no Copilot)."""

import asyncio
import sys
import types

import pytest

from relaymcp.host import voice, voice_warm


class AssistantMessageData:
    def __init__(self, content, tool_requests=None):
        self.content, self.tool_requests = content, tool_requests


class SessionIdleData:
    pass


class SessionErrorData:
    def __init__(self, message):
        self.message = message


class Ev:
    def __init__(self, data):
        self.data = data


class FakeSession:
    script = None  # list of event data per send, set by tests

    def __init__(self, kw):
        self.kw, self.handlers, self.sent = kw, [], []

    def on(self, handler):
        self.handlers.append(handler)
        return lambda: self.handlers.remove(handler)

    async def send(self, text):
        if FakeSession.fail_send:
            raise ConnectionError("runtime went away")
        self.sent.append(text)

        async def emit():
            for data in FakeSession.script:
                await asyncio.sleep(0)
                for h in list(self.handlers):
                    h(Ev(data))
        asyncio.get_running_loop().create_task(emit())

    async def abort(self):
        pass

    async def disconnect(self):
        pass


class FakeClient:
    instances = []

    def __init__(self, connection=None, log_level=None):
        self.connection, self.sessions, self.reject = connection, [], []
        FakeClient.instances.append(self)

    async def start(self):
        pass

    async def stop(self):
        pass

    async def create_session(self, **kw):
        for word in self.reject:
            if word in kw:
                raise RuntimeError(f'Model "x" does not support {word.replace("_", " ")}')
        s = FakeSession(kw)
        self.sessions.append(s)
        return s


@pytest.fixture
def sdk(monkeypatch):
    copilot = types.ModuleType("copilot")
    copilot.CopilotClient = FakeClient
    copilot.RuntimeConnection = types.SimpleNamespace(for_stdio=lambda path=None: ("stdio", path))
    session_mod = types.ModuleType("copilot.session")
    session_mod.PermissionHandler = types.SimpleNamespace(approve_all=object())
    events = types.ModuleType("copilot.session_events")
    events.AssistantMessageData, events.SessionIdleData, events.SessionErrorData = (
        AssistantMessageData, SessionIdleData, SessionErrorData)
    monkeypatch.setitem(sys.modules, "copilot", copilot)
    monkeypatch.setitem(sys.modules, "copilot.session", session_mod)
    monkeypatch.setitem(sys.modules, "copilot.session_events", events)
    FakeClient.instances.clear()
    FakeSession.fail_send = False
    tool = types.SimpleNamespace(name="ally-handheld-handheld_status")
    FakeSession.script = [AssistantMessageData("", [tool]), AssistantMessageData("Your battery is at 90 percent."),
                          SessionIdleData()]
    runtime = voice_warm.WarmRuntime()
    yield runtime
    runtime.stop()


@pytest.fixture
def cfg(relay_home):
    from relaymcp.host import config
    return config.load()


def test_session_options(cfg):
    opts = voice_warm.session_options(cfg, ["github", "speedread"])
    assert set(opts["mcp_servers"]) == {"ally", "ally-handheld"} == set(opts["available_tools"])
    assert all(s["deferTools"] == "never" and s["type"] == "http" for s in opts["mcp_servers"].values())
    assert opts["mcp_servers"]["ally-handheld"]["url"] == "http://127.0.0.1:8767/mcp"
    assert opts["tool_search"] == {"enabled": False} and opts["skip_custom_instructions"] is True
    assert opts["system_message"]["mode"] == "replace" and "`ally-handheld`" in opts["system_message"]["content"]
    assert opts["disabled_mcp_servers"] == ["github", "speedread"]
    assert opts["model"] == "gpt-5.4-mini" and opts["reasoning_effort"] == "low"


def test_drop_rejected():
    opts = {"model": "claude-haiku-4.5", "reasoning_effort": "low", "x": 1}
    assert voice_warm.drop_rejected(opts, 'Model "claude-haiku-4.5" does not support reasoning effort') == \
        {"model": "claude-haiku-4.5", "x": 1}
    assert voice_warm.drop_rejected(opts, "model gpt-9 is not available") == {"x": 1}
    assert voice_warm.drop_rejected(opts, "network unreachable") is None


def test_enabled_only_for_copilot_in_handheld_mode(cfg, monkeypatch):
    monkeypatch.setattr(voice_warm, "sdk_available", lambda: True)
    assert voice_warm.enabled(cfg)
    for key, value in (("permissions", "full"), ("agent", "custom"), ("warm", False)):
        assert not voice_warm.enabled({**cfg, "voice": {**cfg["voice"], key: value}})
    monkeypatch.setattr(voice_warm, "sdk_available", lambda: False)
    assert not voice_warm.enabled(cfg)


def test_ask_reuses_the_session_for_follow_ups(sdk, cfg):
    reply, tools = sdk.ask(cfg, "/bin/copilot", "battery?", "sid-1", True, [], 30)
    assert reply == "Your battery is at 90 percent." and tools == ["ally-handheld-handheld_status"]
    client = FakeClient.instances[0]
    assert client.connection == ("stdio", "/bin/copilot") and sdk.state() == "warm"
    sdk.ask(cfg, "/bin/copilot", "and the volume?", "sid-1", False, [], 30)
    assert len(client.sessions) == 1 and client.sessions[0].sent == ["battery?", "and the volume?"]
    assert client.sessions[0].kw["session_id"] == "sid-1"
    sdk.ask(cfg, "/bin/copilot", "new topic", "sid-2", True, [], 30)
    assert len(client.sessions) == 2 and len(FakeClient.instances) == 1  # same runtime, new session


def test_rejected_settings_are_dropped(sdk, cfg):
    sdk.ask(cfg, "/bin/copilot", "warm up", "sid-0", True, [], 30)
    client = FakeClient.instances[0]
    client.reject = ["reasoning_effort"]
    sdk.ask(cfg, "/bin/copilot", "hi", "sid-1", True, [], 30)
    assert "reasoning_effort" not in client.sessions[-1].kw and client.sessions[-1].kw["model"] == "gpt-5.4-mini"


def test_failures_before_and_after_sending(sdk, cfg):
    FakeSession.fail_send = True
    with pytest.raises(ConnectionError):  # never reached the model: safe to retry with copilot -p
        sdk.ask(cfg, "/bin/copilot", "hi", "sid-1", True, [], 30)
    assert sdk.state() == "failed" and not sdk.usable()
    fresh = voice_warm.WarmRuntime()
    FakeSession.fail_send = False
    FakeSession.script = [SessionErrorData("model overloaded")]
    with pytest.raises(voice_warm.SentError, match="overloaded"):  # the model had it: don't run it twice
        fresh.ask(cfg, "/bin/copilot", "hi", "sid-1", True, [], 30)
    fresh.stop()


def test_run_prompt_prefers_the_warm_runtime(cfg, monkeypatch):
    monkeypatch.setattr(voice, "find_agent", lambda name="copilot": "/bin/copilot")
    monkeypatch.setattr(voice_warm, "enabled", lambda c: True)
    calls = []

    def fake_ask(c, exe, text, sid, fresh, others, timeout):
        calls.append(text)
        return "Done that.", ["ally-handheld-set_volume"]

    monkeypatch.setattr(voice_warm.RUNTIME, "ask", fake_ask)
    monkeypatch.setattr(voice_warm.RUNTIME, "_failed_at", 0.0)
    out = voice.run_prompt(cfg, "set the volume to 40")
    assert out["ok"] and out["runtime"] == "warm" and out["reply"] == "Done that." and calls == ["set the volume to 40"]

    def sent_fail(*a):
        raise voice_warm.SentError("model overloaded")

    monkeypatch.setattr(voice_warm.RUNTIME, "ask", sent_fail)
    monkeypatch.setattr(voice, "copilot_command", lambda *a: pytest.fail("must not re-run a prompt the model had"))
    out = voice.run_prompt(cfg, "set the volume to 40")
    assert not out["ok"] and "overloaded" in out["error"]
