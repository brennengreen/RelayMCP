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


def test_lean_schema_and_results():
    import asyncio
    from relaymcp.device.lean import compact, lean_result, lean_schema
    schema = {"type": "object", "title": "gamepad_holdArguments", "required": ["duration_ms"], "properties": {
        "duration_ms": {"type": "integer", "title": "Duration Ms", "default": 500},
        "buttons": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}], "default": None,
                    "title": "Buttons"}}}
    lean = lean_schema(schema)
    assert "title" not in str(lean) and lean["required"] == ["duration_ms"]
    assert lean["properties"]["buttons"] == {"type": "array", "items": {"type": "string"}}
    assert lean["properties"]["duration_ms"] == {"type": "integer", "default": 500}
    assert compact({"a": None, "b": [{"c": None, "d": 1}]}) == {"b": [{"d": 1}]}

    async def tool():
        return {"ok": True, "gone": None, "nested": {"x": 1.5}}
    assert asyncio.run(lean_result(tool)()) == '{"ok":true,"nested":{"x":1.5}}'


def test_invisible_focus_holders_are_named():
    from relaymcp.device.focus import invisible, warning_for
    hotplug = {"hwnd": 9, "process": "AsHotplugCtrl.exe", "app": "AsHotplugCtrl.exe", "rect": [0, 0, 0, 0], "visible": True}
    assert invisible(hotplug)
    w = warning_for(hotplug, None)
    assert w and "AsHotplugCtrl" in w and "invisible" in w and "finger tap" in w
    hidden = {"hwnd": 8, "process": "helper.exe", "app": "helper.exe", "rect": [0, 0, 400, 300], "visible": False}
    assert invisible(hidden) and "invisible" in warning_for(hidden, None)
    game = {"hwnd": 1, "process": "Minecraft.Windows.exe", "app": "Minecraft.Windows.exe", "rect": [0, 0, 1920, 1080],
            "visible": True}
    assert not invisible(game) and warning_for(game, {"hwnd": 1}) is None
    assert not invisible(None)
