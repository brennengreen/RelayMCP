"""Reading text drawn in a game's bitmap font (a HUD's numbers) exactly, in about a millisecond. OCR engines misread
pixel fonts: on the Ally, Windows OCR read Minecraft's "Position: -11, 100, 0" as "Position: -11, 13B," (its zero
has a slash) every time. A pixel font is drawn at a whole-number scale, so it can be read back glyph by glyph:
threshold the text colour, find the font's pixel size from the thinnest strokes, sample one value per font pixel,
split glyphs at empty columns and match each against the font's bitmaps."""

from __future__ import annotations

import re

# Glyph bitmaps (rows top to bottom, "#" = ink), as drawn by each game. Minecraft's were read off the Ally's screen
# ("-", "1", "3", "0", ",", ":") or follow the same font; the ones not seen yet are checked by width and a distance limit.
FONTS: dict[str, dict[str, tuple[str, ...]]] = {
    "minecraft": {
        "0": (".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."),
        "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", "#####"),
        "2": (".###.", "#...#", "....#", "..##.", ".#...", "#...#", "#####"),
        "3": (".###.", "#...#", "....#", "..##.", "....#", "#...#", ".###."),
        "4": ("...##", "..#.#", ".#..#", "#...#", "#####", "....#", "....#"),
        "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
        "6": ("..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."),
        "7": ("#####", "#...#", "....#", "...#.", "..#..", "..#..", "..#.."),
        "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
        "9": (".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."),
        "-": (".....", ".....", ".....", "#####", ".....", ".....", "....."),
        ",": (".", ".", ".", ".", ".", "#", "#"),
        ":": (".", "#", "#", ".", ".", "#", "#"),
    },
}
ROWS = 7  # glyph height in font pixels (descenders below are ignored)


def _np():
    import numpy
    return numpy


def ink_mask(img, threshold: int = 245):
    """Text pixels: every colour channel at least `threshold` (white HUD text is pure white; its shadow and the box
    behind are darker, but bright sky seen through the box reached 200 on the Ally). img: BGRA/BGR/gray array."""
    np = _np()
    a = np.asarray(img)
    if a.ndim == 3:
        return (a[..., :3] >= threshold).all(axis=2)
    return a >= threshold


def font_scale(mask) -> int:
    """Screen pixels per font pixel: the thinnest horizontal strokes (a glyph's single-pixel columns)."""
    np = _np()
    runs = []
    for row in mask[mask.any(axis=1)]:
        edges = np.flatnonzero(np.diff(np.concatenate(([0], row.astype(np.int8), [0]))))
        runs.extend((edges[1::2] - edges[0::2]).tolist())
    if not runs:
        return 0
    runs.sort()
    return max(1, int(np.median(runs[:max(1, len(runs) // 4)])))


def glyph_grid(mask, scale: float) -> list[str]:
    """The text as font pixels: one character per font pixel, sampled at each pixel's middle. The scale may be
    fractional (a HUD scaled by a non-whole factor: Minecraft's hotbar counts are ~3.7 px per font pixel)."""
    np = _np()
    rows, cols = np.flatnonzero(mask.any(axis=1)), np.flatnonzero(mask.any(axis=0))
    if not len(rows):
        return []
    top, left = int(rows[0]), int(cols[0])
    h = max(1, int(round((int(rows[-1]) - top + 1) / scale)))
    w = max(1, int(round((int(cols[-1]) - left + 1) / scale)))
    ys = np.minimum((top + (np.arange(h) + 0.5) * scale).astype(int), mask.shape[0] - 1)
    xs = np.minimum((left + (np.arange(w) + 0.5) * scale).astype(int), mask.shape[1] - 1)
    sub = mask[np.ix_(ys, xs)]
    return ["".join("#" if v else "." for v in r) for r in sub]


def split_glyphs(grid: list[str]) -> list[tuple[int, list[str]]]:
    """Glyphs (their first column and bitmap) separated by empty columns; a gap of 3+ columns counts as a space
    (returned as (x, []))."""
    if not grid:
        return []
    width = max(len(r) for r in grid)
    rows = [r.ljust(width, ".") for r in grid]
    ink = [any(r[x] == "#" for r in rows) for x in range(width)]
    out, x, gap = [], 0, 0
    while x < width:
        if not ink[x]:
            gap += 1
            x += 1
            continue
        if gap >= 3 and out:
            out.append((x, []))
        gap, start = 0, x
        while x < width and ink[x]:
            x += 1
        out.append((start, [r[start:x] for r in rows]))
    return out


def match_glyph(bitmap: list[str], font: dict[str, tuple[str, ...]], max_wrong: float = 0.12) -> str:
    """The font glyph closest to a bitmap of the same width (Hamming distance over the glyph's rows), or "?"."""
    if not bitmap:
        return " "
    width = len(bitmap[0])
    best, best_d = "?", None
    for ch, rows in font.items():
        if len(rows[0]) != width:
            continue
        d = sum(a != b for r, g in zip(rows, bitmap[:ROWS]) for a, b in zip(r, g))
        if best_d is None or d < best_d:
            best, best_d = ch, d
    if best_d is None or best_d > max(1, int(max_wrong * width * ROWS)):
        return "?"
    return best


def read(img, font: str = "minecraft", threshold: int = 245) -> str:
    """The text in an image region drawn in a known pixel font ("?" for glyphs it doesn't know, e.g. letters). The
    font's pixel size is tried three ways (the thinnest strokes; the text's height as 7 rows, or 8 with a descender)
    and the reading with the fewest unknown glyphs wins."""
    np = _np()
    mask = ink_mask(img, threshold)
    run = font_scale(mask)
    if not run:
        return ""
    rows = np.flatnonzero(mask.any(axis=1))
    height = int(rows[-1]) - int(rows[0]) + 1
    candidates = [float(run)]
    for c in (height / ROWS, height / (ROWS + 1)):
        if c >= 1.0 and all(abs(c - o) > 0.05 for o in candidates):
            candidates.append(c)
    glyphs = FONTS[font]
    best = None
    for scale in candidates:
        text = "".join(match_glyph(bm, glyphs) for _x, bm in split_glyphs(glyph_grid(mask, scale)))
        key = (text.count("?"), -len(text.strip()))
        if best is None or key < best[0]:
            best = (key, text)
    return best[1]


def numbers(text: str) -> list[int]:
    """Whole numbers in text read by `read`, after its last ":" if it has one ("Position: -13, 100, 1")."""
    tail = text.rsplit(":", 1)[-1]
    return [int(m) for m in re.findall(r"-?\d+", tail.replace(" ", ""))]
