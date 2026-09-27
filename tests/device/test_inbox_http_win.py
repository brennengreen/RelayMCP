"""The state inbox's HTTP route and tool on the built hardware server (no network). Run with RELAYMCP_DEVICE_TESTS=1."""

import asyncio
import json
import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RELAYMCP_DEVICE_TESTS") != "1", reason="Windows device tests (CI)")


def test_post_then_read_with_the_tool():
    from starlette.testclient import TestClient

    from relaymcp.device import server
    mcp = server.build_server(18798)
    client = TestClient(mcp.streamable_http_app(), base_url="http://127.0.0.1:18798")
    assert client.post("/state/player", json={"x": 3}).status_code == 403  # no header: refused
    r = client.post("/state/player", json={"x": 3, "health": 17}, headers={"X-Relay-State": "1"})
    assert r.status_code == 200 and r.json()["ok"]
    assert client.post("/state/bad topic", json={}, headers={"X-Relay-State": "1"}).status_code in (400, 404)
    out = asyncio.run(mcp.call_tool("state", {"action": "read", "topic": "player"}))
    blocks = out[0] if isinstance(out, tuple) else out
    data = json.loads([b for b in blocks if b.type == "text"][-1].text)
    assert data["latest"]["player"] == {"x": 3, "health": 17}
