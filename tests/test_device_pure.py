"""Device logic that doesn't need Windows."""


def test_match_score_prefers_exact_process_then_title():
    from relaymcp.device.focus import match_score
    assert match_score("minecraft.windows", "Minecraft", "Minecraft.Windows.exe") == 100
    assert match_score("Minecraft", "Minecraft", "Minecraft.Windows.exe") == 90
    assert match_score("mine", "Minecraft", "Minecraft.Windows.exe") == 60
    assert match_score("craft", "Minecraft", "x.exe") == 40
    assert match_score("steam", "Minecraft", "Minecraft.Windows.exe") == 0


def test_warnings_explain_lost_input():
    from relaymcp.device.focus import short, warning_for
    game = {"hwnd": 1, "process": "Minecraft.Windows.exe", "app": "Minecraft.Windows.exe", "title": "Minecraft"}
    notice = {"hwnd": 2, "process": "ExternalControllerHelper.exe", "title": "GamepadCustomizeExtCtrlr"}
    assert warning_for(game, {"hwnd": 1}) is None
    assert "Armoury Crate" in warning_for(notice, {"hwnd": 1})
    assert "isn't in front" in warning_for({**game, "hwnd": 3, "process": "notepad.exe", "app": "notepad.exe"},
                                           {"hwnd": 1, "title": "Minecraft"})
    assert "desktop" in warning_for({"hwnd": 4, "desktop": True}, None)
    assert warning_for(None, None)
    assert short(game, {"hwnd": 1}) == {"app": "Minecraft.Windows", "title": "Minecraft", "target": True}


def test_pad_unplug_policy():
    from relaymcp.device.gamepad import should_unplug
    assert should_unplug(1800, 1800, pinned=False, game_in_front=False)
    assert not should_unplug(1800, 1800, pinned=False, game_in_front=True)   # never mid-game
    assert not should_unplug(1800, 1800, pinned=True, game_in_front=False)   # kept plugged
    assert not should_unplug(10, 1800, pinned=False, game_in_front=False)
    assert not should_unplug(10 ** 6, None, pinned=False, game_in_front=False)  # 0 minutes = never
