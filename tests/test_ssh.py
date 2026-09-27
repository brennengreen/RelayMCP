def test_ssh_config_and_include(relay_home):
    from relaymcp.host import config, sshconf
    cfg = config.load()
    cfg["device"].update(name="ally", host="192.168.1.23", user="Sam")
    assert sshconf.write_ssh_config(cfg)
    text = config.SSH_CONFIG.read_text()
    assert "Host ally" in text and "HostName 192.168.1.23" in text and "HostKeyAlias relaymcp-ally" in text
    assert "StrictHostKeyChecking yes" in text
    user = sshconf.user_ssh_config().read_text()
    assert user.splitlines()[1].startswith("Include ")
    assert not sshconf.ensure_include()  # idempotent
    assert sshconf.remove_include()
    assert "Include" not in sshconf.user_ssh_config().read_text()


def test_trust_host_key_replaces_old_entry(relay_home):
    from relaymcp.host import config, sshconf
    cfg = config.load()
    config.HOME.mkdir(parents=True, exist_ok=True)
    sshconf.trust_host_key(cfg, "ssh-ed25519 AAAAold")
    sshconf.trust_host_key(cfg, "ssh-ed25519 AAAAnew")
    lines = config.KNOWN_HOSTS.read_text().splitlines()
    assert lines == ["relaymcp-ally ssh-ed25519 AAAAnew"]
