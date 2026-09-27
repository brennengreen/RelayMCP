import importlib
import sys

import pytest


@pytest.fixture(autouse=True)
def _no_dev_mode(monkeypatch):
    """Tests start outside dev mode even when run from a dev shell (scripts/dev-env.sh); dev-mode tests opt in."""
    monkeypatch.delenv("RELAYMCP_DEV", raising=False)
    monkeypatch.delenv("RELAYMCP_DEV_ALLOW", raising=False)


@pytest.fixture(autouse=True)
def _no_real_handheld(tmp_path, monkeypatch):
    """No test may reach a real handheld: ssh reads ~/.ssh/config from the account (not $HOME), so a developer's
    RelayMCP host entry would otherwise work from any test. Every ssh/scp RelayMCP starts is a missing program."""
    monkeypatch.setenv("RELAYMCP_SSH", str(tmp_path / "ssh-is-disabled-in-tests"))


@pytest.fixture()
def relay_home(tmp_path, monkeypatch):
    """A throwaway ~/.relaymcp (and HOME) so tests never touch the real one."""
    monkeypatch.setenv("RELAYMCP_HOME", str(tmp_path / "relaymcp"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))  # Path.home() on Windows
    (tmp_path / "home").mkdir()
    for name in [m for m in sys.modules if m.startswith("relaymcp.host")]:
        del sys.modules[name]
    from relaymcp.host import config
    importlib.reload(config)
    return tmp_path
