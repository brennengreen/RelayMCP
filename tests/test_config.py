import json
import os


def test_defaults_and_roundtrip(relay_home):
    from relaymcp.host import config
    cfg = config.load()
    assert cfg["device"]["ports"] == {"screen": 8765, "hardware": 8767, "voice": 8768}
    assert cfg["voice"]["permissions"] == "handheld"
    cfg["device"]["name"] = "deck"
    token = config.ensure_token(cfg)
    assert len(token) >= 6 and token == config.ensure_token(cfg)
    config.save(cfg)
    again = config.load()
    assert again["device"]["name"] == "deck" and again["enroll"]["token"] == token
    if os.name != "nt":
        assert config.CONFIG_FILE.stat().st_mode & 0o077 == 0  # private


def test_merge_keeps_new_defaults(relay_home):
    from relaymcp.host import config
    config.HOME.mkdir(parents=True)
    config.CONFIG_FILE.write_text(json.dumps({"device": {"name": "ally", "ports": {"screen": 9000}}}))
    cfg = config.load()
    assert cfg["device"]["ports"] == {"screen": 9000, "hardware": 8767, "voice": 8768}
    assert cfg["voice"]["voice"] == "af_heart"


def test_mcp_server_names(relay_home):
    from relaymcp.host import config
    cfg = config.load()
    cfg["device"]["name"] = "legion"
    names = [n for n, _, _ in config.mcp_servers(cfg)]
    assert names == ["legion", "legion-handheld"]
