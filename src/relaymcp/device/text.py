"""Turning chat replies into something pleasant to hear (pure Python, testable anywhere)."""

from __future__ import annotations

import re


def speakable(text: str, limit: int = 480) -> str:
    """Turn a chat reply into something pleasant to hear."""
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"https?://\S+", "a link", t)
    t = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s+", "", t, flags=re.M)
    t = re.sub(r"[*_#>|]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        cut = t[:limit]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = (cut[: end + 1] if end > limit // 3 else cut.rsplit(" ", 1)[0] + "...") + " The rest is on screen."
    return t


_SENTENCE_END = re.compile(r"(?<=[.!?\u2026])[\"'\u201d\u2019)\]]*\s+")


def chunks(text: str, first_max: int = 80, max_len: int = 240) -> list[str]:
    """Split text into sentence-sized pieces; the first is kept short so speech starts quickly."""
    out: list[str] = []
    for part in (p.strip() for p in _SENTENCE_END.split(text.strip())):
        while len(part) > max_len:
            cut = max(part.rfind(", ", 0, max_len), part.rfind("; ", 0, max_len), part.rfind(": ", 0, max_len))
            if cut < max_len // 3:
                cut = part.rfind(" ", 0, max_len)
            if cut <= 0:
                cut = max_len - 1
            out.append(part[: cut + 1].strip())
            part = part[cut + 1:].strip()
        if part:
            out.append(part)
    if out and len(out[0]) > first_max:
        first = out[0]
        cut = first.find(", ", 20, first_max)
        if cut > 0:
            out[0:1] = [first[: cut + 1], first[cut + 2:]]
    merged: list[str] = []
    for c in out:  # fold tiny fragments ("Okay.") into the next piece, except the first
        if len(merged) > 1 and len(merged[-1]) < 20 and len(merged[-1]) + len(c) < max_len:
            merged[-1] = merged[-1] + " " + c
        else:
            merged.append(c)
    return merged
