"""`relaymcp bench`: latency and context cost of the handheld's tools, measured end to end through the tunnel.

Nothing here sends input unless --input is given (that plugs the virtual gamepad in and out once). Results are saved
to ~/.relaymcp/state/bench/ and compared with the previous run.
"""

from __future__ import annotations

import json
import statistics as st
import time
import urllib.request

import relaymcp

from . import config, sshconf, ui

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BENCH_DIR = config.STATE_DIR / "bench"


def _rpc(url: str, method: str, params: dict | None = None, timeout: float = 60) -> tuple[float, dict, int]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params else {})}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    t0 = time.perf_counter()
    raw = _NO_PROXY.open(req, timeout=timeout).read().decode()
    dt = time.perf_counter() - t0
    msg = next((json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")), None) or json.loads(raw)
    return dt, msg, len(raw)


def _call(url: str, tool: str, args: dict | None = None, timeout: float = 60) -> tuple[float, dict, int]:
    return _rpc(url, "tools/call", {"name": tool, "arguments": args or {}}, timeout)


def _stats(samples: list[float]) -> dict:
    s = sorted(samples)
    return {"n": len(s), "p50_ms": round(st.median(s) * 1000, 1), "p95_ms": round(s[min(len(s) - 1, int(len(s) * 0.95))] * 1000, 1),
            "min_ms": round(s[0] * 1000, 1)}


def image_tokens(width: int, height: int) -> int:
    """Rough vision-token cost of an image (~750 pixels per token after fitting within 1568 px, the common scheme)."""
    scale = min(1.0, 1568 / max(width, height, 1))
    return int(width * scale * height * scale / 750)


def result_tokens(msg: dict) -> dict:
    """Estimate the tokens a tool result costs the model: text ~4 chars/token, images by their pixel size."""
    content = (msg.get("result") or {}).get("content") or []
    text = sum(len(c.get("text", "")) for c in content if c.get("type") == "text")
    images = [c for c in content if c.get("type") == "image"]
    img_tokens = 0
    for c in images:
        try:
            import base64
            import struct
            data = base64.b64decode(c.get("data", ""))
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                w, h = struct.unpack(">II", data[16:24])
            elif data[:2] == b"\xff\xd8":
                w, h = _jpeg_size(data)
            else:
                w, h = 1568, 882
            img_tokens += image_tokens(w, h)
        except Exception:
            img_tokens += 1800
    return {"text_chars": text, "images": len(images), "est_tokens": text // 4 + img_tokens}


def _jpeg_size(data: bytes) -> tuple[int, int]:
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker, size = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + size
    return 1568, 882


def run(cfg: dict, rounds: int = 15, with_input: bool = False, with_screen: bool = True) -> dict:
    ports = cfg["device"]["ports"]
    hw, screen = f"http://127.0.0.1:{ports['hardware']}/mcp", f"http://127.0.0.1:{ports['screen']}/mcp"
    out: dict = {"version": relaymcp.__version__, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "device": cfg["device"]["name"]}

    ui.step("MCP round trip (JSON-RPC ping through the tunnel)")
    for label, url in (("hardware", hw), ("screen", screen)):
        try:
            _rpc(url, "ping")
            out[f"ping_{label}"] = _stats([_rpc(url, "ping")[0] for _ in range(rounds)])
            ui.ok(f"{label}: p50 {out[f'ping_{label}']['p50_ms']} ms, p95 {out[f'ping_{label}']['p95_ms']} ms")
        except Exception as e:
            ui.fail(f"{label}: {e}")

    ui.step("Tool definitions (context each session pays)")
    for label, url in (("hardware", hw), ("screen", screen)):
        try:
            _, msg, _ = _rpc(url, "tools/list")
            tools = msg["result"]["tools"]
            chars = len(json.dumps(tools))
            out[f"tools_{label}"] = {"count": len(tools), "chars": chars, "est_tokens": chars // 4}
            ui.ok(f"{label}: {len(tools)} tools, ~{chars // 4} tokens")
        except Exception as e:
            ui.fail(f"{label}: {e}")

    ui.step("Calls")
    calls = [("handheld_status", hw, "handheld_status", {}), ("gamepad_status", hw, "gamepad_status", {})]
    if with_screen:
        calls += [("screenshot", screen, "Screenshot", {}),            # Windows-MCP (the `ally` server)
                  ("screenshot_fast", hw, "screenshot", {}),           # RelayMCP's DXGI capture
                  ("observe", hw, "observe", {})]                      # OCR text of the screen
    for label, url, tool, args in calls:
        try:
            samples, last = [], {}
            for _ in range(3 if label == "screenshot" else 5):
                dt, last, _ = _call(url, tool, args)
                if (last.get("result") or {}).get("isError"):
                    raise RuntimeError("".join(c.get("text", "") for c in last["result"].get("content", []))[:160])
                samples.append(dt)
            out[label] = {**_stats(samples), **result_tokens(last)}
            ui.ok(f"{label}: p50 {out[label]['p50_ms']} ms, ~{out[label]['est_tokens']} tokens per result")
        except Exception as e:
            ui.fail(f"{label}: {e}")

    ui.step("SSH command (relaymcp exec)")
    try:
        samples = []
        for _ in range(3):
            t0 = time.perf_counter()
            sshconf.run(cfg, "$null", timeout=30)
            samples.append(time.perf_counter() - t0)
        out["ssh_exec"] = _stats(samples)
        ui.ok(f"p50 {out['ssh_exec']['p50_ms']} ms")
    except Exception as e:
        ui.fail(str(e))

    if with_input:
        ui.step("Virtual gamepad: plug in to ready (then unplug)")
        try:
            _call(hw, "gamepad_unplug")
            dt, msg, _ = _call(hw, "gamepad_connect", {"keep_plugged": False})
            text = "".join(c.get("text", "") for c in (msg.get("result") or {}).get("content") or [])
            info = json.loads(text) if text.startswith("{") else {}
            out["gamepad_connect"] = {"ms": round(dt * 1000), "ready_after_ms": info.get("ready_after_ms"),
                                      "focus_restored": info.get("focus_restored"), "foreground": info.get("foreground")}
            _call(hw, "gamepad_unplug")
            ui.ok(f"{out['gamepad_connect']['ms']} ms (ready after {info.get('ready_after_ms')} ms)")
        except Exception as e:
            ui.fail(str(e))
    return out


def save_and_compare(result: dict) -> dict | None:
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    runs = sorted(BENCH_DIR.glob("bench-*.json"))
    previous = json.loads(runs[-1].read_text(encoding="utf-8")) if runs else None
    stamp, n = time.strftime("%Y%m%d-%H%M%S"), 0
    while (BENCH_DIR / f"bench-{stamp}{f'-{n}' if n else ''}.json").exists():
        n += 1
    (BENCH_DIR / f"bench-{stamp}{f'-{n}' if n else ''}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return previous


def compare_lines(new: dict, old: dict) -> list[str]:
    lines = []
    for key in sorted(set(new) & set(old)):
        a, b = old[key], new[key]
        if not (isinstance(a, dict) and isinstance(b, dict)):
            continue
        for metric in ("p50_ms", "est_tokens"):
            if metric in a and metric in b and a[metric]:
                change = (b[metric] - a[metric]) / a[metric] * 100
                lines.append(f"{key} {metric}: {a[metric]} -> {b[metric]} ({change:+.0f}%)")
    return lines


def main(cfg: dict, rounds: int, with_input: bool, with_screen: bool, as_json: bool) -> None:
    result = run(cfg, rounds, with_input, with_screen)
    previous = save_and_compare(result)
    if as_json:
        print(json.dumps(result, indent=1))
    if previous:
        ui.step(f"Compared with {previous.get('at')} (relaymcp {previous.get('version')})")
        for line in compare_lines(result, previous):
            ui.info(line)
