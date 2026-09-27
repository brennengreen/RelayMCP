import plistlib


def test_launchd_plist_is_valid(relay_home):
    from relaymcp.host import services
    data = plistlib.loads(services.launchd_plist().encode())
    assert data["Label"] == services.LABEL
    assert data["ProgramArguments"][1:] == ["-m", "relaymcp", "daemon"]
    assert data["KeepAlive"] is True and "PATH" in data["EnvironmentVariables"]


def test_systemd_unit(relay_home):
    from relaymcp.host import services
    unit = services.systemd_unit()
    assert "-m relaymcp daemon" in unit and "Restart=always" in unit and "WantedBy=default.target" in unit
