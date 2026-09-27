import pytest

from relaymcp.host import agents


@pytest.fixture
def cfg():
    return {"device": {"name": "ally", "ports": {"screen": 8765, "hardware": 8767}}}


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "COPILOT_AGENTS", tmp_path)
    return tmp_path


def test_profile_limits_tools_and_sets_model(cfg):
    text = agents.handheld_agent(cfg)
    front = text.split("---")[1]
    assert 'tools: ["relay-hw/*", "relay-screen/*"]' in front
    assert 'relay-hw:\n    type: http\n    url: "http://127.0.0.1:8767/mcp"' in front
    assert 'relay-screen:\n    type: http\n    url: "http://127.0.0.1:8765/mcp"' in front
    assert front.count("deferTools: never") == 2
    assert f"model: {agents.DEFAULT_AGENT_MODEL}" in front
    assert "name: handheld" in front and "description:" in front
    cfg["agent"] = {"model": "gpt-5.4-mini"}
    assert "model: gpt-5.4-mini" in agents.handheld_agent(cfg)
    assert len(text) < 4000  # it's in the subagent's context on every step


def test_install_refresh_and_remove(cfg, agents_dir):
    assert agents.install_agent(cfg) is True
    assert agents.agent_installed() and agents.agent_current(cfg)
    assert agents.install_agent(cfg) is False
    cfg["agent"] = {"model": "gpt-5.4-mini"}
    assert not agents.agent_current(cfg)
    assert agents.install_agent(cfg) is True
    assert agents.remove_agent() is True and not agents.agent_path().exists()
    assert agents.remove_agent() is False


def test_never_overwrites_a_users_own_agent(cfg, agents_dir):
    agents.agent_path().write_text("---\nname: handheld\ndescription: mine\n---\nmy agent\n")
    with pytest.raises(RuntimeError, match="wasn't written by RelayMCP"):
        agents.install_agent(cfg)
    assert agents.remove_agent() is False
    assert "my agent" in agents.agent_path().read_text()


def test_dev_mode_blocks_install(cfg, agents_dir, monkeypatch):
    monkeypatch.setenv("RELAYMCP_DEV", "1")
    with pytest.raises(RuntimeError, match="dev mode"):
        agents.install_agent(cfg)
