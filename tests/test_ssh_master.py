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


def test_master_is_detached_confirmed_and_close_only_stops_new_sessions(cfg, monkeypatch, tmp_path):
    from relaymcp.host import sshconf
    stale = tmp_path / "cm-stale"
    stale.write_text("")
    runs, popens, checks = [], [], iter([False, False, True])

    def fake_run(cmd, **kw):
        runs.append(cmd)
        if "-G" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"hostname 10.0.0.9\ncontrolpath {stale}\n")
        if "check" in cmd:
            return subprocess.CompletedProcess(cmd, 0 if next(checks) else 255)
        return subprocess.CompletedProcess(cmd, 0)

    class FakeProc:
        def __init__(self, cmd, **kw):
            popens.append((cmd, kw))

        def poll(self):
            return None

        def kill(self):
            raise AssertionError("must not kill a master that came up")

    monkeypatch.setattr(sshconf.subprocess, "run", fake_run)
    monkeypatch.setattr(sshconf.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(sshconf.time, "sleep", lambda s: None)
    assert sshconf.ensure_master(cfg) is True
    assert not stale.exists()  # the dead master's socket was cleared first
    cmd, kw = popens[0]
    assert "ControlMaster=yes" in cmd and "-N" in cmd and kw["start_new_session"] is True
    sshconf.close_master(cfg)
    assert runs[-1][-3:] == ["-O", "stop", "ally"]  # not "exit": sessions using it keep running


def test_a_master_that_never_listens_is_killed(cfg, monkeypatch):
    from relaymcp.host import sshconf
    killed = []

    class FakeProc:
        def __init__(self, cmd, **kw):
            pass

        def poll(self):
            return None

        def kill(self):
            killed.append(True)

    monkeypatch.setattr(sshconf, "control_path", lambda cfg: None)
    monkeypatch.setattr(sshconf, "master_running", lambda cfg: False)
    monkeypatch.setattr(sshconf.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(sshconf.time, "sleep", lambda s: None)
    assert sshconf.start_master(cfg) is False and killed == [True]


def test_dev_mode_never_opens_the_real_connection(cfg, monkeypatch):
    from relaymcp.host import sshconf
    monkeypatch.setenv("RELAYMCP_DEV", "1")
    with pytest.raises(RuntimeError, match="dev mode"):
        sshconf.start_master(cfg)
