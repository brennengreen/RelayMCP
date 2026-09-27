"""The persistent PowerShell session. Runs wherever a PowerShell exists (pwsh on macOS/Linux, powershell.exe on
Windows); the handheld uses Windows PowerShell 5.1."""

import os
import shutil

import pytest

from relaymcp.device import pshost

EXE = "powershell.exe" if os.name == "nt" and shutil.which("powershell.exe") else shutil.which("pwsh")
pytestmark = pytest.mark.skipif(not EXE, reason="no PowerShell on this machine")


@pytest.fixture(scope="module")
def ps():
    host = pshost.PowerShellHost(EXE)
    yield host
    host.stop()


def test_state_persists_between_calls_and_calls_are_fast(ps):
    first = ps.run("$relay = 41; function Add-One($n) { $n + 1 }; 'set'")
    assert first["output"] == "set" and first["new_session"]
    second = ps.run("Add-One $relay")
    assert second["output"] == "42" and "new_session" not in {k for k, v in second.items() if v}
    assert second["ms"] < 1500


def test_errors_and_warnings_are_plain_text(ps):
    out = ps.run("Write-Warning 'careful'; Write-Error 'it broke'; 'after'")["output"].splitlines()
    assert "WARNING: careful" in out and "ERROR: it broke" in out and out[-1] == "after"
    assert "<Objs" not in ps.run("Get-Item /definitely/not/here")["output"]
    assert ps.run("throw 'boom'")["output"] == "ERROR: boom"


def test_objects_are_formatted_and_multiline_scripts_work(ps):
    out = ps.run("$x = [pscustomobject]@{ Name = 'ally'; Battery = 87 }\n$x | Format-List")["output"]
    assert "Name" in out and "ally" in out and "87" in out
    assert ps.run("'héllo ✓'")["output"] == "héllo ✓"


def test_timeout_resets_the_session(ps):
    ps.run("$keep = 'x'")
    with pytest.raises(RuntimeError, match="reset"):
        ps.run("Start-Sleep -Seconds 5", timeout=1)
    after = ps.run("if ($keep) { 'kept' } else { 'fresh' }")
    assert after["output"] == "fresh" and after["new_session"]


def test_reset_and_limits(ps):
    ps.run("$v = 1")
    assert ps.run("if ($v) { 'still' } else { 'gone' }", reset=True)["output"] == "gone"
    with pytest.raises(ValueError, match="KB"):
        ps.run("x" * (pshost.MAX_SCRIPT + 1))
    text, clipped = pshost.clip([f"line {i}" for i in range(5000)], limit=1000)
    assert clipped and "..." in text and len(text) < 1100 and text.endswith("line 4999")
