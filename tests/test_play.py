"""relaymcp play: a game on this computer for real-time agents (the play extra)."""

import builtins
import io
import zipfile

import pytest

from relaymcp.host import cli


def test_play_without_the_game_says_how_to_get_it(monkeypatch):
    real = builtins.__import__

    def without_vizdoom(name, *args, **kwargs):
        if name == "vizdoom":
            raise ImportError("No module named 'vizdoom'", name="vizdoom")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_vizdoom)
    with pytest.raises(SystemExit, match=r'pip install "relaymcp\[play\]"'):
        cli.main(["play", "doom", "--headless", "--seconds", "1"])


def test_the_handheld_never_gets_the_games(relay_home):
    from relaymcp.host import kit
    names = zipfile.ZipFile(io.BytesIO(kit.build_device_zip())).namelist()
    assert not [n for n in names if n.startswith("relaymcp/play/")]
