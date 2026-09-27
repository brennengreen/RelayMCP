"""Read-only checks of the handheld's tools after an update: no input is sent (the proc check runs a windowless ping,
the behavior check only watches). Run after scripts/rollout.sh:

    python scripts/validate_device.py
"""
import json
import sys
import time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))
from relaymcp.host import bench, config  # noqa: E402
HW = f"http://127.0.0.1:{config.load()['device']['ports']['hardware']}/mcp"

def call(tool, args=None, timeout=90):
    dt, msg, _ = bench._call(HW, tool, args or {}, timeout=timeout)
    res = msg.get("result") or {}
    text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
    try:
        data = json.loads(text)
    except ValueError:
        data = text
    return dt * 1000, bool(res.get("isError")), data

_, _, listing = None, None, bench._rpc(HW, "tools/list")[1]["result"]["tools"]
print("tools:", len(listing), "| new present:", sorted({"act", "observe", "screenshot", "proc", "powershell", "behavior", "focus_window", "state"} - {t["name"] for t in listing}) or "all")
ms, err, st = call("handheld_status")
print(f"handheld_status {ms:.0f} ms: version {st.get('server_version')}, tools {st.get('tools')}")
for script in ("$PSVersionTable.PSVersion.ToString()", "$x = 40; 'set'", "$x + 2", "Get-Item C:\\nope"):
    ms, err, out = call("powershell", {"script": script})
    print(f"powershell {ms:5.0f} ms err={err}: {str(out)[:140]}")
ms, err, out = call("proc", {"action": "start", "name": "pingtest", "command": "ping -n 3 127.0.0.1", "pattern": "Reply from", "timeout": 10})
print(f"proc start {ms:.0f} ms err={err}: ready={out.get('ready') if isinstance(out, dict) else out}")
ms, err, out = call("proc", {"action": "wait", "name": "pingtest", "pattern": "Packets: Sent", "timeout": 10})
print(f"proc wait {ms:.0f} ms: {out.get('matched') if isinstance(out, dict) else out}")
ms, err, out = call("proc", {"action": "list"})
print("proc list:", [(s["name"], s["running"], s["exit_code"]) for s in out.get("sessions", [])] if isinstance(out, dict) else out)
ms, err, out = call("behavior", {"action": "start", "kind": "watch", "params": {"region": [0, 0, 400, 300], "when": {"change": 3}, "every_ms": 50}, "max_s": 3})
rid = out.get("id") if isinstance(out, dict) else None
time.sleep(3.5)
ms, err, out = call("behavior", {"action": "status", "id": rid})
print(f"behavior watch: {out if not isinstance(out, dict) else {k: out.get(k) for k in ('state', 'reason', 'hz', 'frame_ms_p50', 'events')}}")
ms, err, out = call("focus_window", {})
print(f"focus_window() {ms:.0f} ms: fg={out.get('foreground')} last_app={out.get('last_app_window')} changes={len(out.get('focus_changes') or [])} idle={out.get('seconds_since_input')}")
ms, err, out = call("state", {"action": "topics"})
print(f"state topics {ms:.0f} ms: {out}")
ms, err, out = call("gamepad_status")
print(f"gamepad_status {ms:.0f} ms: virtual {out.get('virtual_gamepad') if isinstance(out, dict) else out}")
