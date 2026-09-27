"""Reading a game's pixel font exactly (Minecraft's HUD coordinates), from grids seen on the Ally and synthetic renders."""

import pytest

np = pytest.importorskip("numpy")

from relaymcp.device import pixfont  # noqa: E402

# "Position: -13, 100, 1" as read off the Ally's screen (4 screen px per font pixel), font pixels
SEEN = [
    "####..............#..#..#...........................#....###..........#....###...###..........#..",
    "#...#................#................#............##...#...#........##...#...#.#...#........##..",
    "####...###...####.#.###.#..###..####..#.............#.......#.........#...#..##.#..##.........#..",
    "#.....#...#.#.....#..#..#.#...#.#...#.......#####...#.....##..........#...#.#.#.#.#.#.........#..",
    "#.....#...#..###..#..#..#.#...#.#...#...............#.......#.........#...##..#.##..#.........#..",
    "#.....#...#.....#.#..#..#.#...#.#...#.#.............#...#...#.#.......#...#...#.#...#.#.......#..",
    "#......###..####..#...#.#..###..#...#.#...........#####..###..#.....#####..###...###..#.....#####",
    "..............................................................#.......................#..........",
]


def render(grid, scale, shadow=True, background=40, noise=0, seed=0):
    """Draw font pixels as a HUD does: white ink, a darker shadow one font pixel down-right, a dim box behind."""
    rng = np.random.default_rng(seed)
    h, w = len(grid) + 2, len(grid[0]) + 2
    img = np.full((h * scale, w * scale, 4), background, np.uint8)
    if noise:
        img[..., :3] = np.clip(img[..., :3] + rng.integers(-noise, noise, img[..., :3].shape), 0, 180)
    for y, row in enumerate(grid):
        for x, ch in enumerate(row):
            if ch == "#":
                if shadow:
                    img[(y + 2) * scale:(y + 3) * scale, (x + 2) * scale:(x + 3) * scale, :3] = 63
    for y, row in enumerate(grid):
        for x, ch in enumerate(row):
            if ch == "#":
                img[(y + 1) * scale:(y + 2) * scale, (x + 1) * scale:(x + 2) * scale, :3] = 255
    return img


def text_grid(s, font="minecraft"):
    glyphs = pixfont.FONTS[font]
    cols = []
    for ch in s:
        if ch == " ":
            cols.extend(["." * 7] * 3)
            continue
        rows = glyphs[ch]
        for x in range(len(rows[0])):
            cols.append("".join(r[x] for r in rows))
        cols.append("." * 7)
    return ["".join(c[y] for c in cols) for y in range(7)]


def test_the_coordinates_seen_on_the_ally_read_exactly():
    img = render(SEEN, 4)
    assert pixfont.font_scale(pixfont.ink_mask(img)) == 4
    text = pixfont.read(img)
    assert text.endswith(":-13, 100, 1".replace(" ", " ")) or pixfont.numbers(text) == [-13, 100, 1], text
    assert pixfont.numbers(text) == [-13, 100, 1]


@pytest.mark.parametrize("scale", [2, 3, 4])
def test_every_digit_reads_back_at_any_scale_through_shadow_and_noise(scale):
    s = "-1234567890, 9876, -5"
    img = render(text_grid(s), scale, noise=30, seed=scale)
    assert pixfont.read(img) == s.replace(" ", " ")
    assert pixfont.numbers("Position: " + pixfont.read(img)) == [-1234567890, 9876, -5]


def test_letters_it_doesnt_know_dont_become_digits():
    grid = [r[:40] for r in SEEN]  # "Position:"
    text = pixfont.read(render(grid, 4))
    assert text.endswith(":") and not any(c.isdigit() for c in text), text
    assert pixfont.numbers(text) == []


def test_programs_read_hud_numbers():
    from relaymcp.device import behave

    img = render(SEEN, 4)

    class Hud:
        def grab(self, region=None):
            a = img if not region else img[region[1]:region[3], region[0]:region[2]]
            return a, 0.0

    class Out:
        def release(self, used=()):
            pass

    rt = behave.Runtime(Hud().grab, Out())
    out = behave.run_tool(rt, "start", "program", {"code": "result = numbers([0, 0, W, H])", "wait": True}, max_s=5)
    assert out["state"] == "done" and out["result"] == [-13, 100, 1], out



def render_scaled(grid, scale, background=40):
    """Font pixels drawn at a fractional scale (nearest neighbour), white on a dim box, no shadow."""
    h, w = int(round((len(grid) + 2) * scale)), int(round((len(grid[0]) + 2) * scale))
    img = np.full((h, w, 4), background, np.uint8)
    ys, xs = np.mgrid[0:h, 0:w]
    fy, fx = (ys / scale).astype(int) - 1, (xs / scale).astype(int) - 1
    inside = (fy >= 0) & (fy < len(grid)) & (fx >= 0) & (fx < len(grid[0]))
    ink = np.zeros((h, w), bool)
    ink[inside] = [grid[a][b] == "#" for a, b in zip(fy[inside], fx[inside])]
    img[ink, :3] = 255
    return img


@pytest.mark.parametrize("scale", [3.7, 2.6, 4.3])
def test_hotbar_counts_drawn_at_a_fractional_scale_read_right(scale):
    """Minecraft's hotbar counts are drawn ~3.7 screen px per font pixel: sampling on a whole-pixel grid drifts a
    font pixel within two glyphs ("20" read as "?")."""
    for s in ("20", "17", "5", "64", "38"):
        assert pixfont.read(render_scaled(text_grid(s), scale)) == s, (s, scale)
