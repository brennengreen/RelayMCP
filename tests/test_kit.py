import io
import re
import zipfile


def _cfg(relay_home):
    from relaymcp.host import config, sshconf
    config.HOME.mkdir(parents=True, exist_ok=True)
    config.KEY_FILE.write_text("private")
    config.KEY_FILE.with_suffix(".pub").write_text("ssh-ed25519 AAAATESTKEY relaymcp@test\n")
    cfg = config.load()
    cfg["device"].update(name="ally", home_networks=["Sam's Wi-Fi"], home_gateways=["AA-BB-CC-11-22-33"])
    cfg["host_label"] = "Sam\u2019s Mac"
    assert sshconf.public_key().startswith("ssh-ed25519")
    return cfg


def test_render_fills_every_placeholder(relay_home):
    from relaymcp.host import kit
    text = kit.render_setup(_cfg(relay_home), "10.0.0.5", "mac.local")
    assert not re.findall(r"__[A-Z]+__", text)
    assert "$DeviceName       = 'ally'" in text
    assert "$HomeNetworkNames = @('Sam''s Wi-Fi')" in text  # single quotes doubled for PowerShell
    assert "$HomeGateways     = @('AA-BB-CC-11-22-33')" in text
    assert "http://10.0.0.5:8766/done/" in text and "http://mac.local:8766/done/" in text
    assert "$ScreenPort = 8765" in text  # the controller gets the ports too


def test_device_zip_is_deterministic_and_installable(relay_home):
    from relaymcp.host import kit
    a, b = kit.build_device_zip(), kit.build_device_zip()
    assert a == b
    names = zipfile.ZipFile(io.BytesIO(a)).namelist()
    assert "pyproject.toml" in names
    assert "relaymcp/device/agent.py" in names and "relaymcp/device/setup/Relay-Setup.ps1" in names
    assert "relaymcp/device/assets/ask-copilot.ico" in names
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
    assert not [n for n in names if n.startswith("relaymcp/host/")]  # host changes don't change the device build
    pyproject = zipfile.ZipFile(io.BytesIO(a)).read("pyproject.toml").decode()
    assert 'name = "relaymcp-device"' in pyproject and '"faster-whisper' in pyproject
    assert 'relaymcp-ask = "relaymcp.device.launcher:main"' in pyproject


def test_device_requirements_match_extra(relay_home):
    from relaymcp.host import kit
    reqs = kit.device_requirements()
    assert any(r.startswith("mcp") for r in reqs) and any(r.startswith("kokoro-onnx") for r in reqs)


def test_a_checkout_ships_its_own_device_requirements_not_stale_install_metadata(relay_home, monkeypatch):
    import importlib.metadata as md
    from relaymcp.host import kit
    before = kit.build_device_zip()
    # an editable install from before capture and OCR were added: its metadata lists fewer device dependencies
    monkeypatch.setattr(md, "requires", lambda name: ['mcp<2,>=1.12; extra == "device"', 'numpy>=1.26; extra == "device"'])
    reqs = kit.device_requirements()
    assert any(r.startswith("dxcam") for r in reqs) and any(r.startswith("pillow") for r in reqs)
    assert kit.build_device_zip() == before  # the same code, the same build, whichever install builds it


def test_launcher_is_crlf_ascii():
    from relaymcp.host import kit
    text = kit.launcher_text("your Mac")
    assert text.count("\r\n") == 4 and "Relay-Setup.ps1" in text


def test_powershell_escaping_handles_typographic_quotes():
    from relaymcp.host import kit
    assert kit.ps_quote("Sam's Mac") == "'Sam''s Mac'"
    assert kit.ps_quote("Sam\u2019s Mac") == "'Sam\u2019\u2019s Mac'"
    assert kit.ps_list(["a", "b\u2018c"]) == "'a', 'b\u2018\u2018c'"
