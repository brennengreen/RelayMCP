import base64
import re


def test_script_command_carries_script_and_args_safely():
    from relaymcp.host import cli
    text = "param($a, $b)\n'got ' + $a + '|' + $b  # quotes ' \" and $vars survive\n"
    cmd = cli.script_command(text, ["two words", "it's"])
    b64 = re.search(r"FromBase64String\('([^']+)'\)", cmd).group(1)
    assert base64.b64decode(b64).decode() == text
    assert "@('two words', 'it''s')" in cmd and "@__relay_args" in cmd


def test_exec_file_and_stdin(relay_home, monkeypatch, tmp_path):
    from relaymcp.host import cli, config, sshconf
    config.save(config.load())
    sent = []
    monkeypatch.setattr(sshconf, "powershell", lambda cfg, script, timeout=0: sent.append(script) or
                        type("R", (), {"stdout": "ok\n", "stderr": "", "returncode": 0})())
    script = tmp_path / "t.ps1"
    script.write_text("Write-Output 'hi'")
    try:
        cli.main(["exec", "--file", str(script), "--", "x"])
    except SystemExit as e:
        assert e.code == 0
    assert "FromBase64String" in sent[-1] and "@('x')" in sent[-1]


def test_runtime_check():
    from relaymcp.host.doctor import runtime_check
    assert runtime_check("ABC123", "0.1.1", {"hash": "abc123", "version": "0.1.1"}).ok
    # `relaymcp deploy` doesn't rewrite device.json's version; identical builds are the same version
    assert "0.1.1" in runtime_check("abc123", "0.1.1", {"hash": "abc123", "version": "0.1.0"}).detail
    stale = runtime_check("abc123", "0.1.1", {"hash": "fff000", "version": "0.1.0"})
    assert not stale.ok and "fff000" in stale.detail and "0.1.0" not in stale.detail and "relaymcp deploy" in stale.fix
    assert not runtime_check("abc", "0.1.1", {}).ok
