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
    return lines_from(result.lines, left, top)


def find(lines: list[dict], query: str) -> dict | None:
    """The best line for query: exact match, then a line starting with it, then one containing it (case-insensitive)."""
    q = " ".join(query.lower().split())
    if not q:
        return None
    ranked = []
    for i, line in enumerate(lines):
        t = " ".join(line["text"].lower().split())
        score = 3 if t == q else 2 if t.startswith(q) else 1 if q in t else 0
        if score:
            ranked.append((-score, len(t), i, line))
    return min(ranked)[3] if ranked else None
