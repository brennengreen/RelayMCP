import io

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from relaymcp.device import capture  # noqa: E402


def _frame(w=1920, h=1080, box=(100, 100, 500, 400), bgra=(0, 0, 255, 0), new=True):
    """A black screen with a red (BGRA 0,0,255) rectangle."""
    row = bytearray(w * 4)
    data = bytearray(row * h)
    x0, y0, x1, y1 = box
    px = bytes(bgra)
    for y in range(y0, y1):
        data[(y * w + x0) * 4:(y * w + x1) * 4] = px * (x1 - x0)
    return capture.Frame(bytes(data), w, h, 0, 0, "test", 0.0, new)


def test_whole_factor():
    assert capture.whole_factor(1920, 1080, 960) == 2
    assert capture.whole_factor(1920, 1080, 1920) == 1
    assert capture.whole_factor(1920, 1080, 1280) == 2
    assert capture.whole_factor(1920, 1080, 500) == 4
    assert capture.whole_factor(400, 300, 960) == 1


def test_clamp_region():
    assert capture.clamp_region(None, 0, 0, 1920, 1080) is None
    assert capture.clamp_region([10, 20, 30, 40], 0, 0, 1920, 1080) == (10, 20, 30, 40)
    assert capture.clamp_region([-50, -50, 5000, 5000], 0, 0, 1920, 1080) == (0, 0, 1920, 1080)
    with pytest.raises(ValueError, match="outside the screen"):
        capture.clamp_region([3000, 0, 4000, 100], 0, 0, 1920, 1080)


def test_mapping_words():
    assert capture.mapping(1, 0, 0) == "same as screen"
    assert capture.mapping(2, 0, 0) == "screen = (x*2, y*2)"
    assert capture.mapping(1, 400, 300) == "screen = (x+400, y+300)"
    assert capture.mapping(3, 10, 0) == "screen = (x*3+10, y*3)"


def test_encode_downscales_and_keeps_colors():
    jpeg, meta = capture.encode(_frame())
    assert jpeg[:2] == b"\xff\xd8" and meta == {"image": [960, 540], "to_screen": "screen = (x*2, y*2)"}
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    r, g, b = img.getpixel((150, 125))  # inside the rectangle, in image coordinates
    assert r > 200 and g < 60 and b < 60, (r, g, b)  # BGRA red came out red
    assert max(img.getpixel((600, 400))) < 40
    assert len(jpeg) < 60_000


def test_encode_region_is_full_detail():
    jpeg, meta = capture.encode(_frame(), region=[100, 100, 500, 400])
    assert meta == {"image": [400, 300], "to_screen": "screen = (x+100, y+100)"}


class FakeCam:
    def __init__(self, frames):
        self.frames = list(frames)

    def grab(self, new_frame_only=True):
        return self.frames.pop(0) if self.frames else None

    def release(self):
        pass


def test_dxgi_unchanged_frames_reuse_the_last_image(monkeypatch):
    np = pytest.importorskip("numpy")
    arr = np.zeros((1080, 1920, 4), dtype=np.uint8)
    g = capture.Grabber()
    g._cam = FakeCam([arr, None])
    first = g._grab_dxgi()
    second = g._grab_dxgi()
    assert first.new and not second.new and second.data is first.data and second.at >= first.at


def test_fallback_to_gdi_and_retry_later(monkeypatch):
    g = capture.Grabber()
    calls = []

    def dxgi():
        calls.append("dxgi")
        raise RuntimeError("no duplication")

    monkeypatch.setattr(g, "_grab_dxgi", dxgi)
    monkeypatch.setattr(g, "_grab_gdi", lambda: _frame())
    now = [1000.0]
    monkeypatch.setattr(capture.time, "monotonic", lambda: now[0])
    assert g.grab().backend == "test" and calls == ["dxgi"]
    assert g.grab().backend == "test" and calls == ["dxgi"]  # within DXGI_RETRY_S: GDI only
    now[0] += capture.DXGI_RETRY_S + 1
    g.grab()
    assert calls == ["dxgi", "dxgi"]


def test_only_if_changed(monkeypatch):
    g = capture.Grabber()
    frames = [_frame(), _frame(), _frame(box=(600, 600, 900, 900))]
    monkeypatch.setattr(g, "grab", lambda: frames.pop(0))
    first = g.screenshot(only_if_changed=True)
    assert first["jpeg"] and first["meta"]["source"] == "test"
    second = g.screenshot(only_if_changed=True)
    assert second["jpeg"] is None and second["meta"]["unchanged"] is True
    assert g.screenshot(only_if_changed=True)["jpeg"]


def test_content_lists_pass_through_lean_result():
    import asyncio

    pytest.importorskip("mcp")
    from mcp.types import ImageContent, TextContent

    from relaymcp.device.lean import is_content, lean_result

    blocks = [ImageContent(type="image", data="AA==", mimeType="image/jpeg"), TextContent(type="text", text="{}")]
    assert is_content(blocks) and not is_content([{"a": 1}]) and not is_content([])

    async def tool():
        return blocks

    assert asyncio.run(lean_result(tool)()) is blocks


class _R:
    def __init__(self, x, y, w, h):
        self.x, self.y, self.width, self.height = x, y, w, h


class _W:
    def __init__(self, rect):
        self.bounding_rect = rect


class _L:
    def __init__(self, text, rects):
        self.text, self.words = text, [_W(r) for r in rects]


def test_ocr_lines_and_find():
    from relaymcp.device import ocr
    lines = ocr.lines_from([_L("Launch Mission", [_R(10, 20, 80, 30), _R(95, 22, 90, 28)]), _L("", []),
                            _L("Settings", [_R(10, 80, 70, 25)])], left=100, top=50)
    assert lines == [{"text": "Launch Mission", "box": [110, 70, 285, 100]},
                     {"text": "Settings", "box": [110, 130, 180, 155]}]
    assert ocr.center(lines[0]["box"]) == [197, 85]
    assert ocr.find(lines, "launch")["text"] == "Launch Mission"
    assert ocr.find(lines, "SETTINGS")["text"] == "Settings"
    assert ocr.find(lines, "mission")["text"] == "Launch Mission"
    assert ocr.find(lines, "nothing") is None and ocr.find(lines, "  ") is None
    exact = ocr.find([{"text": "Play Online", "box": [0] * 4}, {"text": "Play", "box": [1] * 4}], "play")
    assert exact["text"] == "Play"


def test_ocr_crop_bgra():
    np = pytest.importorskip("numpy")
    from relaymcp.device import ocr
    f = _frame(w=64, h=32, box=(8, 4, 16, 12))
    data, w, h, left, top = ocr.crop_bgra(f, (8, 4, 16, 12))
    assert (w, h, left, top) == (8, 8, 8, 4) and len(data) == 8 * 8 * 4
    assert np.frombuffer(data, np.uint8).reshape(8, 8, 4)[0, 0].tolist() == [0, 0, 255, 0]


def test_merge_rows_joins_split_phrases_but_not_separate_buttons():
    from relaymcp.device import ocr
    lines = [{"text": "Launch Mission", "box": [420, 238, 596, 274]},
             {"text": "Button", "box": [388, 352, 492, 386]},       # one label that OCR split in two
             {"text": "pressed", "box": [502, 355, 634, 389]},
             {"text": "Play", "box": [100, 600, 160, 630]},         # two buttons in a row, far apart
             {"text": "Settings", "box": [400, 600, 520, 630]}]
    merged = ocr.merge_rows(lines)
    texts = [ln["text"] for ln in merged]
    assert texts == ["Launch Mission", "Button pressed", "Play", "Settings"]
    assert merged[1]["box"] == [388, 352, 634, 389]
    assert ocr.find(merged, "Button pressed")["text"] == "Button pressed"
    assert ocr.merge_rows([]) == []
