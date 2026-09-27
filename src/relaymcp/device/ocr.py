"""Text on screen via Windows' built-in OCR engine (Windows.Media.Ocr): no model files, and fast enough to run on every
observation. Lines come back with boxes in screen pixels, so an agent can read the screen as text and tap what it
reads. Call from one thread (server.SCREEN, which is COM-initialized)."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("relaymcp.ocr")

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from winrt.windows.media.ocr import OcrEngine
        _engine = OcrEngine.try_create_from_user_profile_languages()
        if _engine is None:
            raise RuntimeError("Windows has no OCR language installed (Settings > Time & language > Language & region: "
                               "add English with its optional features)")
    return _engine


def crop_bgra(frame, box=None):
    """(bytes, width, height, left, top) of the frame, or of box = (left, top, right, bottom) in screen pixels."""
    import numpy as np

    arr = frame.data if hasattr(frame.data, "shape") else np.frombuffer(frame.data, np.uint8)
    arr = arr.reshape(frame.height, frame.width, 4)
    if box:
        x0, y0, x1, y1 = (box[0] - frame.left, box[1] - frame.top, box[2] - frame.left, box[3] - frame.top)
        arr = arr[y0:y1, x0:x1]
        left, top = box[0], box[1]
    else:
        left, top = frame.left, frame.top
    arr = np.ascontiguousarray(arr)
    return arr.tobytes(), arr.shape[1], arr.shape[0], left, top


def lines_from(ocr_lines, left: int = 0, top: int = 0) -> list[dict]:
    """[{"text", "box": [l, t, r, b]}] in screen pixels from OCR lines (each with .text and .words[].bounding_rect)."""
    out = []
    for line in ocr_lines:
        rects = [w.bounding_rect for w in line.words]
        if not rects:
            continue
        x0 = min(r.x for r in rects)
        y0 = min(r.y for r in rects)
        x1 = max(r.x + r.width for r in rects)
        y1 = max(r.y + r.height for r in rects)
        out.append({"text": line.text, "box": [round(x0) + left, round(y0) + top, round(x1) + left, round(y1) + top]})
    return out


def _same_row(a, b) -> bool:
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    return overlap >= 0.6 * min(a[3] - a[1], b[3] - b[1])


def merge_rows(lines: list[dict], gap_ratio: float = 0.8) -> list[dict]:
    """Join pieces of one visual line: the OCR engine sometimes splits a phrase ("Button" | "pressed") into separate
    lines. Pieces merge when they share a row and the gap between them is under gap_ratio x the text height, which
    keeps separate buttons in a row apart."""
    out = [dict(ln) for ln in lines]
    merged = True
    while merged:
        merged = False
        for i, a in enumerate(out):
            for j, b in enumerate(out):
                if i == j or not _same_row(a["box"], b["box"]):
                    continue
                gap = b["box"][0] - a["box"][2]  # b starts right after a
                height = max(a["box"][3] - a["box"][1], b["box"][3] - b["box"][1])
                if -0.2 * height <= gap <= gap_ratio * height:
                    a["text"] = f"{a['text']} {b['text']}"
                    a["box"] = [min(a["box"][0], b["box"][0]), min(a["box"][1], b["box"][1]),
                                max(a["box"][2], b["box"][2]), max(a["box"][3], b["box"][3])]
                    del out[j]
                    merged = True
                    break
            if merged:
                break
    return out


def center(box) -> list[int]:
    return [(box[0] + box[2]) // 2, (box[1] + box[3]) // 2]


def recognize(frame, box=None) -> list[dict]:
    """OCR lines of the frame (or a box of it), with boxes in screen pixels."""
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap

    data, w, h, left, top = crop_bgra(frame, box)
    bitmap = SoftwareBitmap.create_copy_from_buffer(data, BitmapPixelFormat.BGRA8, w, h)
    engine = _get_engine()

    async def run():
        return await engine.recognize_async(bitmap)

    result = asyncio.run(run())
    return merge_rows(lines_from(result.lines, left, top))


# Letters OCR confuses in blocky game fonts (Minecraft's R reads as "fi", Q as "a"). Both sides are mapped the same
# way before comparing, so a genuine "fi" or "q" still matches itself.
CONFUSIONS = (("fi", "r"), ("q", "a"), ("rn", "m"), ("vv", "w"), ("0", "o"), ("1", "l"), ("|", "l"), ("—", "-"),
              ("’", "'"))


def normalize(text: str) -> str:
    t = " ".join(text.lower().split())
    for a, b in CONFUSIONS:
        t = t.replace(a, b)
    return t


def similarity(query: str, text: str) -> float:
    """How well text contains query, 0-1, tolerating OCR slips in stylized game fonts ("fiesume" for "Resume",
    "auit" for "Quit"): the best match of query against any same-length stretch of text."""
    from difflib import SequenceMatcher
    q, t = query, text
    if not q or not t:
        return 0.0
    if len(t) <= len(q) + 2:
        return SequenceMatcher(None, q, t).ratio()
    best = 0.0
    for start in range(0, len(t) - len(q) + 1):
        best = max(best, SequenceMatcher(None, q, t[start:start + len(q) + 1]).ratio())
    return best


def find(lines: list[dict], query: str, fuzzy: float = 0.8) -> dict | None:
    """The best line for query: exact match, then a line starting with it, then one containing it (case-insensitive),
    then one cut off partway through it, then, for game fonts OCR misreads, the most similar line (at least `fuzzy`
    similar; 0 turns that off)."""
    q = normalize(query)
    if not q:
        return None
    ranked, close = [], []
    for i, line in enumerate(lines):
        t = normalize(line["text"])
        score = 3 if t == q else 2 if t.startswith(q) else 1 if q in t else 0
        if not score and len(t) >= 5 and q.startswith(t[:-1]) and len(t) - 1 >= 0.6 * len(q):
            score = 0.5  # cut off at an edge, last letter half drawn: "Wooden Picl" for "Wooden Pickaxe" (a tooltip)
        if score:
            ranked.append((-score, len(t), i, line))
        elif fuzzy and len(q) >= 4:
            sim = similarity(q, t)
            if sim >= fuzzy:
                close.append((-sim, len(t), i, line))
    if ranked:
        return min(ranked)[3]
    return min(close)[3] if close else None
