import json


def test_new_conversation_regex():
    from relaymcp.host.voice import NEW_CONVERSATION
    assert NEW_CONVERSATION.match("New conversation. What's my battery?").group(1) == "What's my battery?"
    assert NEW_CONVERSATION.match("okay, start over").group(1) == ""
    assert not NEW_CONVERSATION.match("what's new in this conversation")


def test_only_local_programs_allowed():
    from relaymcp.host.voice import VOICE_HEADER, allowed
    good = {"Host": "127.0.0.1:8768", "Content-Type": "application/json", VOICE_HEADER: "1"}
    assert allowed(good, post=True)
    assert not allowed({**good, "Host": "evil.example:8768"}, post=True)   # DNS rebinding
    assert not allowed({**good, "Content-Type": "text/plain"}, post=True)   # simple cross-site POST
    assert not allowed({k: v for k, v in good.items() if k != VOICE_HEADER}, post=True)
    assert allowed({"Host": "localhost:8768"}, post=False)


def test_header_matches_device():
    from relaymcp.device.paths import VOICE_HEADER as device_header
    from relaymcp.host.voice import VOICE_HEADER as host_header
    assert device_header == host_header


def test_parse_copilot_output():
    from relaymcp.host.voice import parse_copilot_output
    lines = [
        {"type": "assistant.message", "data": {"content": "", "toolRequests": [{"name": "ally-handheld-handheld_status"}]}},
        {"type": "assistant.message", "data": {"content": "Your battery is at 87 percent."}},
        {"type": "result", "exitCode": 0},
    ]
    reply, tools, code = parse_copilot_output("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    assert reply == "Your battery is at 87 percent." and tools == ["ally-handheld-handheld_status"] and code == 0


def test_copilot_command_limits_tools(relay_home, monkeypatch):
    from relaymcp.host import config, voice
    monkeypatch.setattr(voice, "other_copilot_servers", lambda own: ["github", "speedread"])
    cfg = config.load()
    cmd = voice.copilot_command(cfg, "copilot", "hi", "sid", fresh=True)
    assert ["--allow-tool", "ally"] == cmd[cmd.index("--allow-tool"):cmd.index("--allow-tool") + 2]
    assert "--allow-all" not in cmd and cmd.count("--disable-mcp-server") == 2
    cfg["voice"]["permissions"] = "full"
    assert "--allow-all" in voice.copilot_command(cfg, "copilot", "hi", "sid", fresh=False)
