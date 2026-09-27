"""Key holds are counted; failed sends never leave keys counted or release someone else's hold. Windows only (the
module binds user32), with SendInput stubbed out: no real input is sent."""

import os
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="win_input binds user32")


@pytest.fixture
def keys(monkeypatch):
    from relaymcp.device import win_input
    sent, fail = [], set()

    def fake_send(inputs):
        for inp in inputs:
            name, up = inp
            if (name, up) in fail:
                raise OSError(f"SendInput refused {name} {'up' if up else 'down'}")
            sent.append(f"{name}{'↑' if up else '↓'}")

    monkeypatch.setattr(win_input, "_send", fake_send)
    monkeypatch.setattr(win_input, "_key_input", lambda name, up: (name, up))
    win_input._keys_down.clear()
    yield win_input, sent, fail
    win_input._keys_down.clear()


def test_overlapping_holds_send_one_down_and_one_up(keys):
    win_input, sent, _ = keys
    walker = threading.Thread(target=win_input.key_press, args=(["w"], 300))
    walker.start()
    time.sleep(0.1)
    win_input.key_press(["w"], 10)  # a tap of the key being held doesn't let go of it
    assert sent == ["w↓"]
    walker.join()
    assert sent == ["w↓", "w↑"] and not win_input._keys_down


def test_a_failed_key_down_releases_only_what_it_pressed(keys):
    win_input, sent, fail = keys
    holder = threading.Thread(target=win_input.key_press, args=(["shift"], 300))
    holder.start()
    time.sleep(0.1)
    fail.add(("ctrl", False))
    with pytest.raises(OSError):
        win_input.key_press(["shift", "ctrl"], 10)
    assert "shift↑" not in sent  # the other hold of shift continues
    holder.join()
    assert sent[-1] == "shift↑" and not win_input._keys_down


def test_a_failed_key_up_still_releases_the_rest(keys):
    win_input, sent, fail = keys
    fail.add(("c", True))
    win_input.key_press(["ctrl", "c"], 10)
    assert sent == ["ctrl↓", "c↓", "ctrl↑"] and not win_input._keys_down  # nothing left counted as held
    fail.clear()
    win_input.key_press(["ctrl", "v"], 10)
    assert sent[-4:] == ["ctrl↓", "v↓", "v↑", "ctrl↑"]
