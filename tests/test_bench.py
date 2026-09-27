import base64
import json
import struct

from relaymcp.host import bench


def _png(w, h):
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00"


def _jpeg(w, h):
    sof = b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08" + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x03" + b"\x00" * 9
    app0 = b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00" + b"\x00" * 9
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def test_image_tokens_fits_long_side_to_1568():
    assert bench.image_tokens(960, 540) == 960 * 540 // 750
    assert bench.image_tokens(1920, 1080) == bench.image_tokens(1568, 882)


def test_result_tokens_counts_text_and_images():
    msg = {"result": {"content": [
        {"type": "text", "text": "x" * 400},
        {"type": "image", "data": base64.b64encode(_png(1920, 1080)).decode()},
        {"type": "image", "data": base64.b64encode(_jpeg(960, 540)).decode()},
    ]}}
    r = bench.result_tokens(msg)
    assert r["text_chars"] == 400 and r["images"] == 2
    assert r["est_tokens"] == 100 + bench.image_tokens(1920, 1080) + bench.image_tokens(960, 540)


def test_jpeg_size_skips_segments():
    assert bench._jpeg_size(_jpeg(1280, 800)) == (1280, 800)


def test_stats_and_compare():
    s = bench._stats([0.010, 0.020, 0.030, 0.040])
    assert s["n"] == 4 and s["min_ms"] == 10.0 and s["p50_ms"] == 25.0
    lines = bench.compare_lines({"a": {"p50_ms": 50.0}, "b": 1}, {"a": {"p50_ms": 100.0}, "b": 2})
    assert lines == ["a p50_ms: 100.0 -> 50.0 (-50%)"]


def test_save_and_compare_returns_previous(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "BENCH_DIR", tmp_path)
    assert bench.save_and_compare({"at": "one"}) is None
    (tmp_path / "bench-00000000-000000.json").write_text(json.dumps({"at": "zero"}))
    assert bench.save_and_compare({"at": "two"})["at"] == "one"
    assert len(list(tmp_path.glob("bench-*.json"))) == 3


def test_bench_input_is_blocked_in_dev_mode(monkeypatch):
    import argparse

    import pytest

    from relaymcp.host import cli
    monkeypatch.setenv("RELAYMCP_DEV", "1")
    monkeypatch.setattr(cli, "_need_config", lambda: {})
    with pytest.raises(RuntimeError, match="dev mode"):
        cli.cmd_bench(argparse.Namespace(input=True, rounds=1, no_screen=True, json=False))
