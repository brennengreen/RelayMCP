"""Small terminal helpers: consistent, readable output and prompts."""

from __future__ import annotations

import os
import sys

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(text: str) -> str:
    return _c("1", text)


def dim(text: str) -> str:
    return _c("2", text)


def step(msg: str) -> None:
    print(_c("1;36", "==> ") + _c("1", msg), flush=True)


def info(msg: str) -> None:
    print("    " + msg, flush=True)


def ok(msg: str) -> None:
    print("  " + _c("32", "\u2713") + " " + msg, flush=True)


def warn(msg: str) -> None:
    print("  " + _c("33", "!") + " " + msg, flush=True)


def fail(msg: str) -> None:
    print("  " + _c("31", "\u2717") + " " + msg, flush=True)


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask(prompt: str, default: str | None = None) -> str:
    if not interactive():
        return default or ""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"    {prompt}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or (default or "")


def confirm(prompt: str, default: bool = True) -> bool:
    if not interactive():
        return default
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"    {prompt} [{hint}] ").strip().lower()
    except EOFError:
        return default
    return default if not answer else answer in ("y", "yes")
