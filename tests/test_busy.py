import argparse

import pytest

from relaymcp.host import busy

QUIET = {"now": "2026-09-26T21:00:00", "idle_s": 900, "busy_until": "", "awake_until": "", "busy_note": "",
         "ssh_commands": []}


def test_quiet():
    assert busy.reasons(QUIET) == []


def test_recent_tool_call():
    assert busy.reasons({**QUIET, "idle_s": 12}) == ["an MCP tool call 12 s ago"]
    assert busy.reasons({**QUIET, "idle_s": 12}, quiet_s=10) == []


def test_marks_use_the_devices_clock():
    p = {**QUIET, "busy_until": "2026-09-26T21:30:00", "busy_note": "bedrock-live: BDS running"}
    assert busy.reasons(p) == ["marked busy until 21:30 (bedrock-live: BDS running)"]
    assert busy.reasons({**QUIET, "busy_until": "2026-09-26T20:59:00"}) == []  # expired
    lease = {**QUIET, "awake_until": "2026-09-26T22:00:00"}
    assert busy.reasons(lease) == ["a keep-awake lease until 22:00"]
    assert busy.reasons(lease, ignore_lease=True) == []
    assert busy.reasons({**QUIET, "busy_until": "garbage"}) == []


def test_ssh_commands():
    assert busy.reasons({**QUIET, "ssh_commands": "bedrock_server.exe since 19:20"}) == \
        ["running over SSH: bedrock_server.exe since 19:20"]
    many = [f"p{i}.exe since 20:0{i}" for i in range(6)]
    assert busy.reasons({**QUIET, "ssh_commands": many})[0].endswith(" ...")


def test_deploy_refuses_while_busy(monkeypatch):
    from relaymcp.host import cli
    monkeypatch.setattr(cli, "_need_config", lambda: {"device": {"name": "ally"}})
    monkeypatch.setattr(cli, "_need_device", lambda cfg: None)
    monkeypatch.setattr(busy, "probe", lambda cfg: {**QUIET, "ssh_commands": ["bedrock_server.exe since 19:20"]})
    with pytest.raises(SystemExit) as e:
        cli.cmd_deploy(argparse.Namespace(full=False, force=False))
    assert e.value.code == 1


def test_busy_check_exit_codes(monkeypatch, capsys):
    from relaymcp.host import cli
    monkeypatch.setattr(cli, "_need_config", lambda: {"device": {"name": "ally"}})
    monkeypatch.setattr(cli, "_need_device", lambda cfg: None)
    args = argparse.Namespace(minutes=None, note=None, check=True, quiet_seconds=60, ignore_lease=False)
    monkeypatch.setattr(busy, "probe", lambda cfg: QUIET)
    with pytest.raises(SystemExit) as e:
        cli.cmd_busy(args)
    assert e.value.code == 0 and "quiet" in capsys.readouterr().out
    monkeypatch.setattr(busy, "probe", lambda cfg: {**QUIET, "idle_s": 5})
    with pytest.raises(SystemExit) as e:
        cli.cmd_busy(args)
    assert e.value.code == 1 and "busy: an MCP tool call 5 s ago" in capsys.readouterr().out


def test_mark_script_is_safe_with_any_note(monkeypatch):
    import base64
    captured = {}

    def fake(cfg, script, timeout=0):
        captured["s"] = script
        return type("R", (), {"stdout": "ok"})()

    monkeypatch.setattr(busy.sshconf, "powershell", fake)
    busy.mark({}, 30, "it's \"quoted\"; $(evil)")
    s = captured["s"]
    assert "AddMinutes([int]'30')" in s and "$(evil)" not in s
    encoded = s.split("FromBase64String('")[1].split("'")[0]
    assert base64.b64decode(encoded).decode() == "it's \"quoted\"; $(evil)"
