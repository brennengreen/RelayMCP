import importlib
import sys

import pytest


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
