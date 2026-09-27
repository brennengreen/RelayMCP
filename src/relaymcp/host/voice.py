"""Voice dispatcher: runs push-to-talk prompts from the handheld with your AI agent (GitHub Copilot CLI by default).

The handheld reaches this server at its own 127.0.0.1:<voice port>, which the SSH tunnel reverse-forwards to this
computer's 127.0.0.1:<voice port>, so nothing listens on the network. Each prompt runs the agent in a continuing
"voice" session (a new one after 20 idle minutes, or when the request starts with "new conversation"); the final reply
goes back to the handheld to be spoken. One prompt runs at a time. Settings: the "voice" section of
~/.relaymcp/config.json (`relaymcp voice`)."""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, voice_warm

log = logging.getLogger("relaymcp.voice")
VOICE_HEADER = "X-Relay-Voice"  # required on POST /prompt (see Handler._allowed); must match relaymcp.device.paths
STATE_FILE = config.STATE_DIR / "voice-session.json"
NEW_CONVERSATION = re.compile(
    r"^\s*(?:ok(?:ay)?[, ]+)?(?:new conversation|start (?:a )?new conversation|start over|reset(?: the)? conversation)"
    r"[\s,.!?:;-]*(.*)$", re.I | re.S)
EXTRA_PATH = [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]

_busy = threading.Lock()


def find_agent(name: str = "copilot") -> str | None:
    return shutil.which(name, path=os.pathsep.join([*EXTRA_PATH, os.environ.get("PATH", "")]))


def other_copilot_servers(own: tuple[str, ...]) -> list[str]:
    try:
        servers = json.loads((Path.home() / ".copilot" / "mcp-config.json").read_text()).get("mcpServers", {})
    except (OSError, ValueError):
        return []
    return [name for name in servers if name not in own]


def session_for(new: bool, idle_minutes: float) -> tuple[str, bool]:
    state: dict = {}
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        pass
    fresh = new or not state.get("session_id") or time.time() - state.get("last_used", 0) > idle_minutes * 60
    sid = str(uuid.uuid4()) if fresh else state["session_id"]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"session_id": sid, "last_used": time.time()}))
    return sid, fresh


def notify(text: str) -> None:
    title = "RelayMCP voice prompt"
    try:
        if platform.system() == "Darwin":
            safe = text.replace("\\", "\\\\").replace('"', '\\"')[:180]
            subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "{title}"'],
                           capture_output=True, timeout=10)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, text[:180]], capture_output=True, timeout=10)
    except Exception:
        pass


def preamble(cfg: dict, full: bool) -> str:
    name = cfg["device"]["name"]
    who = cfg["voice"].get("user_name") or config.default_user_name()
    reach = (f"You can see and control the handheld with the `{name}` tools (screenshots, clicks, typing, launching "
             f"apps, PowerShell) and the `{name}-handheld` tools (gamepad, touch, keys, audio, speech, display, power).")
    if full:
        reach += " You can also run commands and edit files on this computer."
    else:
        reach += (" You can't run commands or edit files on this computer from a voice request; if that's needed, say "
                  "so briefly and suggest asking from the computer.")
    return (f"[Voice request from {who}, spoken on their handheld gaming PC (push-to-talk) and transcribed by Whisper, "
            "so words may be misheard; interpret sensibly. " + reach +
            " Your final message will be read aloud on the handheld by text-to-speech: answer in one to three short, "
            "conversational sentences of plain text (no markdown, lists, code, or URLs). If you did something, say "
            "what you did.]\n\n")


def tts_settings(cfg: dict) -> dict:
    v = cfg["voice"]
    return {"voice": v.get("voice") or None, "speed": v.get("speech_speed") or 1.0}


def copilot_command(cfg: dict, exe: str, prompt: str, sid: str, fresh: bool) -> list[str]:
    v, name = cfg["voice"], cfg["device"]["name"]
    own = (name, f"{name}-handheld")
    full = v.get("permissions") == "full"
    workdir = Path(os.path.expanduser(v.get("workdir") or config.VOICE_WORKDIR))
    workdir.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-p", preamble(cfg, full) + prompt, "--output-format", "json", "--stream", "off",
           "--session-id", sid, "-C", str(workdir), "--no-custom-instructions"]
    if fresh:
        cmd += ["-n", f"{name} voice " + time.strftime("%b %d %H:%M")]
    if full:
        cmd += ["--allow-all"]
    else:
        # Only the handheld's tools: a much smaller prompt (no GitHub MCP, no built-in tools), so replies come faster.
        cmd += ["--disable-builtin-mcps", "--available-tools", *own]
        for server in own:
            cmd += ["--allow-tool", server]
        for server in other_copilot_servers(own):
            cmd += ["--disable-mcp-server", server]
    # Copilot defers MCP tool schemas behind a search tool, which costs a voice prompt a whole extra model round trip.
    # Re-declaring the hardware server for this run only keeps its schemas in view (other sessions stay deferred).
    urls = {n: url for n, url, _ in config.mcp_servers(cfg)}
    cmd += ["--additional-mcp-config", json.dumps({"mcpServers": {own[1]: {
        "type": "http", "url": urls[own[1]], "tools": ["*"], "deferTools": "never"}}}, separators=(",", ":"))]
    if v.get("model"):
        cmd += ["--model", str(v["model"])]
    if v.get("reasoning_effort"):
        cmd += ["--reasoning-effort", str(v["reasoning_effort"])]
    return cmd


def without_option(cmd: list[str], flag: str) -> list[str]:
    """cmd without `flag` and its value."""
    out, skip = [], False
    for part in cmd:
        if skip:
            skip = False
            continue
        if part == flag:
            skip = True
            continue
        out.append(part)
    return out


def retry_command(cmd: list[str], error: str) -> list[str] | None:
    """A variant of cmd without the option the CLI rejected (a model that isn't available, or one that doesn't take a
    reasoning effort), or None if the error is something else."""
    e = error.lower()
    if "--reasoning-effort" in cmd and "reasoning" in e:
        return without_option(cmd, "--reasoning-effort")
    if "--model" in cmd and "model" in e and any(w in e for w in ("not available", "not found", "unknown", "invalid",
                                                                   "not supported", "unsupported", "no access")):
        return without_option(without_option(cmd, "--model"), "--reasoning-effort")
    return None


def parse_copilot_output(out: str) -> tuple[str, list[str], int | None]:
    """(final reply, tool names used, exit code) from `copilot --output-format json` (JSON lines)."""
    reply, tools, exit_code = "", [], None
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind, data = event.get("type"), event.get("data") or {}
        if kind == "assistant.message":
            for req in data.get("toolRequests") or []:
                tools.append(req.get("name", "?"))
            if (data.get("content") or "").strip():
                reply = data["content"].strip()
        elif kind == "result":
            exit_code = event.get("exitCode", exit_code)
    return reply, tools, exit_code


def run_prompt(cfg: dict, text: str) -> dict:
    v = cfg["voice"]
    m = NEW_CONVERSATION.match(text)
    new = bool(m)
    if m:
        text = m.group(1).strip()
    if not text:  # just "new conversation": the next prompt starts a fresh session
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"session_id": None, "last_used": 0}))
        return {"ok": True, "reply": "Okay, starting a new conversation.", "new_session": True, "tts": tts_settings(cfg)}
    sid, fresh = session_for(new, float(v.get("new_conversation_after_minutes") or 20))
    if v.get("agent", "copilot") == "custom":
        template = v.get("custom_command") or []
        if not template:
            return {"ok": False, "error": "voice.custom_command isn't set"}
        full = v.get("permissions") == "full"
        cmd = [part.replace("{prompt}", preamble(cfg, full) + text).replace("{session}", sid) for part in template]
    else:
        exe = find_agent("copilot")
        if not exe:
            return {"ok": False, "error": "GitHub Copilot CLI isn't installed on this computer"}
        if voice_warm.enabled(cfg) and voice_warm.RUNTIME.usable():
            name, t_warm = cfg["device"]["name"], time.monotonic()
            try:
                reply, tools = voice_warm.RUNTIME.ask(cfg, exe, text, sid, fresh, other_copilot_servers((name, f"{name}-handheld")),
                                                      float(v.get("timeout_minutes") or 10) * 60)
                return {"ok": True, "reply": reply or "Done.", "session": sid, "new_session": fresh, "tools": tools,
                        "seconds": round(time.monotonic() - t_warm, 1), "tts": tts_settings(cfg), "runtime": "warm"}
            except voice_warm.SentError as e:  # the model already had the prompt: don't run it twice
                return {"ok": False, "error": str(e)[:200], "session": sid, "seconds": round(time.monotonic() - t_warm, 1)}
            except Exception as e:
                log.warning("warm voice runtime unavailable, using copilot -p: %s", e)
        cmd = copilot_command(cfg, exe, text, sid, fresh)
    env = dict(os.environ, PATH=os.pathsep.join([*EXTRA_PATH, os.environ.get("PATH", "")]))
    t0 = time.monotonic()
    kwargs: dict = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x08000000}
    timeout_min = float(v.get("timeout_minutes") or 10)
    custom = v.get("agent", "copilot") == "custom"
    for _attempt in range(3):
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, **kwargs)
        try:
            out, err = proc.communicate(timeout=timeout_min * 60)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            return {"ok": False, "error": f"it took longer than {timeout_min:g} minutes", "session": sid}
        if custom:
            reply, tools, exit_code = out.strip(), [], proc.returncode
        else:
            reply, tools, exit_code = parse_copilot_output(out)
            exit_code = proc.returncode if exit_code is None else exit_code
        retry = None if custom or reply or exit_code == 0 else retry_command(cmd, (err or "") + (out or ""))
        if not retry:
            break
        log.info("retrying without an option the agent rejected: %s", (err or out).strip().splitlines()[-1:])
        cmd = retry
    seconds = round(time.monotonic() - t0, 1)
    if exit_code != 0 and not reply:
        detail = (err or out).strip().splitlines()[-1:] or [f"exit code {exit_code}"]
        return {"ok": False, "error": detail[0][:200], "session": sid, "seconds": seconds}
    return {"ok": True, "reply": reply or "Done.", "session": sid, "new_session": fresh, "seconds": seconds,
            "tools": tools, "tts": tts_settings(cfg)}


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass


def allowed(headers, post: bool) -> bool:
    """Only local programs (the handheld's voice client via the tunnel), never web pages: a DNS-rebinding page fails the
    Host check, and a cross-site page can't send the custom header or a JSON body without a CORS preflight, which
    this server never approves."""
    host = (headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        return False
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    return not post or (headers.get(VOICE_HEADER) == "1" and ctype == "application/json")


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if not allowed(self.headers, post=False):
            self._send(403, {"ok": False, "error": "forbidden"})
        elif self.path == "/health":
            cfg = config.load()
            agent = cfg["voice"].get("agent", "copilot")
            runtime = voice_warm.RUNTIME.state() if voice_warm.enabled(cfg) else "cli"
            self._send(200, {"ok": True, "busy": _busy.locked(), "permissions": cfg["voice"].get("permissions"),
                             "agent": agent, "agent_found": agent == "custom" or bool(find_agent("copilot")),
                             "runtime": runtime})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not allowed(self.headers, post=True):
            self._send(403, {"ok": False, "error": "forbidden"})
            return
        if self.path != "/prompt":
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            text = str(json.loads(self.rfile.read(length) or b"{}").get("text", "")).strip()
        except (ValueError, TypeError):
            self._send(400, {"ok": False, "error": "bad request"})
            return
        if not text:
            self._send(400, {"ok": False, "error": "empty prompt"})
            return
        if not _busy.acquire(blocking=False):
            self._send(409, {"ok": False, "error": "busy"})
            return
        try:
            cfg = config.load()
            log.info("prompt (%s): %r", cfg["voice"].get("permissions"), text[:300])
            if cfg["voice"].get("notify", True):
                notify(text)
            result = run_prompt(cfg, text)
            log.info("result: ok=%s %ss tools=%s reply=%r", result.get("ok"), result.get("seconds", "?"),
                     result.get("tools", []), str(result.get("reply") or result.get("error"))[:300])
            self._send(200, result)
        except Exception as e:
            log.exception("voice prompt failed")
            self._send(500, {"ok": False, "error": str(e)[:200]})
        finally:
            _busy.release()

    def log_message(self, fmt: str, *args) -> None:
        pass


def serve(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, name="voice", daemon=True).start()
    return server
