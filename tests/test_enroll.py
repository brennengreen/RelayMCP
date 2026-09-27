import urllib.error
import urllib.request

import pytest


def test_enrollment_requires_token(tmp_path):
    from relaymcp.host import enroll, kit
    for f in kit.KIT_FILES:
        (tmp_path / f).write_bytes(b"data:" + f.encode())
    e = enroll.Enrollment(tmp_path, "tok123", "127.0.0.1", 0)
    e.start()
    port = e._server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        boot = urllib.request.urlopen(f"{base}/tok123").read().decode()
        assert "/kit/tok123/" in boot and "Relay-Setup.ps1" in boot
        assert urllib.request.urlopen(f"{base}/kit/tok123/Relay-Setup.ps1").read() == b"data:Relay-Setup.ps1"
        for bad in ("/wrong", "/kit/wrong/Relay-Setup.ps1", "/kit/tok123/../config.json", "/done/wrong?status=ok"):
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(base + bad)
        assert e.report is None
        urllib.request.urlopen(f"{base}/done/tok123?status=ok&hostkey=ssh-ed25519%20AAAA&ip=10.0.0.9").read()
        report = e.wait(2)
        assert report["status"] == "ok" and report["ip"] == "10.0.0.9" and report["hostkey"] == "ssh-ed25519 AAAA"
    finally:
        e.stop()
