"""Warm voice runtime: one long-lived GitHub Copilot runtime (github-copilot-sdk) answers every voice prompt.

A `copilot -p` run spends ~4-5 s starting up before the model even sees the prompt. Here the runtime is started once
(by the daemon, in the background), each prompt gets a session in ~30 ms with a short voice-only system prompt and the
handheld's tools in view (no tool-search round trip), and follow-ups continue the same session. One-tool prompts
finish in ~3.5 s instead of ~8 s.

Optional: needs Python 3.11+ and `pip install 'relaymcp[voice]'`. Whenever the SDK is missing or anything fails,
voice.run_prompt falls back to the `copilot -p` path.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import threading
import time

from . import config

log = logging.getLogger("relaymcp.voice")

RETRY_AFTER_FAILURE_S = 300  # after the runtime fails, use `copilot -p` for a while before trying again


class SentError(RuntimeError):
    """The prompt reached the model before the failure, so re-running it elsewhere could repeat its actions."""


def sdk_available() -> bool:
    return sys.version_info >= (3, 11) and importlib.util.find_spec("copilot") is not None


def enabled(cfg: dict) -> bool:
    """The warm runtime serves copilot voice prompts in handheld permission mode (full mode keeps the CLI path)."""
    v = cfg["voice"]
    return (v.get("warm", True) and v.get("agent", "copilot") == "copilot" and v.get("permissions") != "full"
            and sdk_available())


def system_prompt(cfg: dict) -> str:
    name = cfg["device"]["name"]
    who = cfg["voice"].get("user_name") or config.default_user_name()
    return (f"You are the voice assistant on {who}'s handheld gaming PC. Requests are spoken (push-to-talk) and "
            "transcribed by Whisper, so words may be misheard; interpret them sensibly. You can see and control the "
            f"handheld with the `{name}` tools (screenshots, clicks, typing, launching apps, PowerShell) and the "
            f"`{name}-handheld` tools (gamepad, touch, keys, audio, speech, display, power; observe and act for "
            "on-screen text and multi-step input)." + games_sentence(name) + _extra_sentence(cfg) +
            " You can't run commands or edit files on the computer; if that's "
            "needed, say so briefly and suggest asking from the computer. Your reply is read aloud: answer in one to "
            "three short, conversational sentences of plain text (no markdown, lists, code or URLs). If you did "
            "something, say what you did.")


def games_sentence(name: str) -> str:
    """A game request ("build me a house here") is something to play, not to decline (it was declined: the prompt
    only listed gamepad buttons). Saved skills make it one call."""
    return (f" To play a game, use the `{name}-handheld` `behavior` tool, which runs real-time programs on the "
            "handheld: check its saved skills (action \"skills\") and start the one that does what was asked by name "
            "(action \"start\", kind \"program\", params.skill), or write a short program; start long tasks and say "
            "you started them rather than waiting for them to finish.")


def _extra_sentence(cfg: dict) -> str:
    extra = config.voice_servers(cfg)[2:]
    return (" You can also use the " + ", ".join(f"`{s}`" for s in extra) + " tools.") if extra else ""


def session_options(cfg: dict, other_servers: list[str]) -> dict:
    """create_session keyword arguments (everything except the permission handler and session id)."""
    v, name = cfg["voice"], cfg["device"]["name"]
    urls = {n: url for n, url, _ in config.mcp_servers(cfg)}
    servers = {n: {"type": "http", "url": urls[n], "tools": ["*"], "deferTools": "never", "timeout": 120000}
               for n in (name, f"{name}-handheld")}
    servers.update({n: {**c, "timeout": 120000} for n, c in config.voice_extra_server_configs(cfg).items()})
    opts: dict = {"mcp_servers": servers, "available_tools": list(servers), "tool_search": {"enabled": False},
                  "system_message": {"mode": "replace", "content": system_prompt(cfg)},
                  "skip_custom_instructions": True, "disabled_mcp_servers": other_servers or None,
                  "working_directory": str(config.VOICE_WORKDIR)}
    if v.get("model"):
        opts["model"] = str(v["model"])
    if v.get("reasoning_effort"):
        opts["reasoning_effort"] = str(v["reasoning_effort"])
    return {k: val for k, val in opts.items() if val is not None}


def drop_rejected(opts: dict, error: str) -> dict | None:
    """opts without the setting the runtime rejected (like voice.retry_command), or None."""
    e = error.lower()
    if "reasoning" in e and "reasoning_effort" in opts:
        return {k: v for k, v in opts.items() if k != "reasoning_effort"}
    if "model" in e and ("not available" in e or "not supported" in e or "unknown" in e) and "model" in opts:
        return {k: v for k, v in opts.items() if k not in ("model", "reasoning_effort")}
    return None


class WarmRuntime:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._exe: str | None = None
        self._session = None
        self._session_id: str | None = None
        self._failed_at = 0.0
        self._attempted = False
        self._start_lock = threading.Lock()

    # --- plumbing: an event loop on its own thread, shared by every prompt -----------------------------------------

    def _run(self, coro, timeout: float):
        with self._start_lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, name="voice-runtime", daemon=True).start()
                self._loop = loop
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def state(self) -> str:
        """For status: warm (runtime running), cold (not started: prompts start it), or failed (using copilot -p)."""
        if self._client is not None:
            return "warm"
        return "failed" if self._failed_at and not self.usable() else "cold"

    def usable(self) -> bool:
        return time.monotonic() - self._failed_at > RETRY_AFTER_FAILURE_S or not self._failed_at

    async def _ensure_client(self, exe: str):
        if self._client is not None and self._exe == exe:
            return self._client
        from copilot import CopilotClient, RuntimeConnection
        await self._stop_client()
        client = CopilotClient(connection=RuntimeConnection.for_stdio(path=exe), log_level="error")
        t0 = time.monotonic()
        await client.start()
        log.info("voice runtime started in %.1fs", time.monotonic() - t0)
        self._client, self._exe = client, exe
        return client

    async def _stop_client(self) -> None:
        client, self._client, self._session, self._session_id = self._client, None, None, None
        if client is not None:
            try:
                await client.stop()
            except Exception:
                pass

    # --- public -----------------------------------------------------------------------------------------------------

    def prewarm(self, exe: str) -> None:
        """Start the runtime in the background (called when the daemon starts)."""
        def go():
            try:
                self._run(self._ensure_client(exe), 60)
            except Exception as e:
                log.warning("voice runtime didn't start (voice prompts use `copilot -p`): %s", e)
                self._failed_at = time.monotonic()
        threading.Thread(target=go, name="voice-prewarm", daemon=True).start()

    def ask(self, cfg: dict, exe: str, text: str, sid: str, fresh: bool, other_servers: list[str],
            timeout: float) -> tuple[str, list[str]]:
        """(reply, tools used). Raises SentError once the prompt may have reached the model (the caller must not run
        it again), and anything else only if it certainly didn't (the caller falls back to `copilot -p`)."""
        self._attempted = False
        try:
            return self._run(self._ask(cfg, exe, text, sid, fresh, other_servers, timeout), timeout + 30)
        except Exception as e:
            self._failed_at = time.monotonic()
            try:
                self._run(self._stop_client(), 15)
            except Exception:
                pass
            if self._attempted and not isinstance(e, SentError):  # e.g. the outer timeout while it was running
                raise SentError(str(e) or type(e).__name__) from e
            raise

    def stop(self) -> None:
        if self._loop is not None:
            try:
                self._run(self._stop_client(), 15)
            except Exception:
                pass

    async def _ask(self, cfg, exe, text, sid, fresh, other_servers, timeout) -> tuple[str, list[str]]:
        from copilot.session import PermissionHandler
        from copilot.session_events import AssistantMessageData, SessionErrorData, SessionIdleData

        client = await self._ensure_client(exe)
        if fresh or self._session is None or self._session_id != sid:
            if self._session is not None:
                try:
                    await self._session.disconnect()
                except Exception:
                    pass
                self._session = None
            opts = session_options(cfg, other_servers)
            for _ in range(3):
                try:
                    self._session = await client.create_session(on_permission_request=PermissionHandler.approve_all,
                                                                session_id=sid, **opts)
                    break
                except Exception as e:
                    opts = drop_rejected(opts, str(e))
                    if opts is None:
                        raise
                    log.info("voice runtime: retrying without a rejected setting (%s)", e)
            self._session_id = sid
        session = self._session
        reply, tools, error, done = "", [], None, asyncio.Event()

        def on_event(ev):
            nonlocal reply, error
            data = ev.data
            if isinstance(data, AssistantMessageData):
                reply = (data.content or "").strip() or reply
                tools.extend(getattr(r, "name", "?") for r in (getattr(data, "tool_requests", None) or []))
            elif isinstance(data, SessionErrorData):
                error = getattr(data, "message", None) or str(data)
                done.set()
            elif isinstance(data, SessionIdleData):
                done.set()

        unsubscribe = session.on(on_event)
        sent = self._attempted = True  # from here on the model may have the prompt, even if send() then fails
        try:
            await session.send(text)
            try:
                await asyncio.wait_for(done.wait(), timeout)
            except asyncio.TimeoutError:
                try:
                    await asyncio.wait_for(session.abort(), 10)
                except Exception:
                    pass
                raise SentError(f"it took longer than {timeout / 60:g} minutes") from None
        except SentError:
            raise
        except Exception as e:
            if sent:
                raise SentError(str(e)) from e
            raise
        finally:
            if callable(unsubscribe):
                unsubscribe()
        if error and not reply:
            raise SentError(error)
        return reply, tools


RUNTIME = WarmRuntime()
