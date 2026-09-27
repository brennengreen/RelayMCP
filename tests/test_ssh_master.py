import os
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="connection sharing isn't used on Windows")


@pytest.fixture
def cfg(relay_home):
    from relaymcp.host import config
    c = config.load()
    c["device"].update({"host": "10.0.0.9", "user": "Sam"})
    return c


def test_commands_never_become_the_shared_connection(cfg):
    from relaymcp.host import sshconf
    text = sshconf.render_ssh_config(cfg)
    assert "  ControlMaster no\n" in text and "ControlPath" in text
    assert "ControlMaster auto" not in text and "ControlPersist" not in text


def test_refresh_only_rewrites_relaymcps_own_file(cfg):
    from relaymcp.host import config, sshconf
    assert sshconf.refresh_ssh_config(cfg) is False  # not set up yet: nothing to refresh
    config.SSH_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    config.SSH_CONFIG.write_text("Host ally\n  ControlMaster auto\n  ControlPersist 10m\n")
    assert sshconf.refresh_ssh_config(cfg) is True
    assert "ControlMaster no" in config.SSH_CONFIG.read_text()
    assert sshconf.refresh_ssh_config(cfg) is False
    assert not (config.Path.home() / ".ssh" / "config").exists()


def test_master_is_detached_and_close_only_stops_new_sessions(cfg, monkeypatch):
    from relaymcp.host import sshconf
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 1 if "check" in cmd else 0)

    monkeypatch.setattr(sshconf.subprocess, "run", fake_run)
    assert sshconf.ensure_master(cfg) is True
    check, start = calls[0][0], calls[1]
    assert check[-3:] == ["-O", "check", "ally"]
    assert "ControlMaster=yes" in start[0] and "ControlPersist=yes" in start[0] and "-f" in start[0]
    assert start[1]["start_new_session"] is True  # survives the background service restarting
    sshconf.close_master(cfg)
    assert calls[-1][0][-3:] == ["-O", "stop", "ally"]  # not "exit": sessions using it keep running


def test_dev_mode_never_opens_the_real_connection(cfg, monkeypatch):
    from relaymcp.host import sshconf
    monkeypatch.setenv("RELAYMCP_DEV", "1")
    with pytest.raises(RuntimeError, match="dev mode"):
        sshconf.start_master(cfg)
