import pytest


def test_dev_mode_blocks_real_systems(relay_home, monkeypatch):
    from relaymcp.host import agents, config, devguard, services, sshconf
    monkeypatch.setenv("RELAYMCP_DEV", "1")
    cfg = config.load()
    for call in (services.install, services.uninstall, services.restart, sshconf.ensure_include,
                 sshconf.remove_include, lambda: agents.register_copilot(cfg), lambda: agents.unregister_copilot(cfg),
                 lambda: sshconf.run(cfg, "hostname"), lambda: sshconf.copy_to(cfg, [], "x")):
        with pytest.raises(RuntimeError, match="dev mode"):
            call()
    monkeypatch.setenv("RELAYMCP_DEV_ALLOW", "1")
    assert not devguard.blocked()


def test_dev_mode_uses_separate_ports(relay_home, monkeypatch):
    from relaymcp.host import config
    monkeypatch.setenv("RELAYMCP_DEV", "1")
    assert config.load()["device"]["ports"] == {"screen": 18765, "hardware": 18767, "voice": 18768}
    assert config.load()["enroll"]["port"] == 18766
    monkeypatch.delenv("RELAYMCP_DEV")
    assert config.load()["device"]["ports"]["screen"] == 8765
