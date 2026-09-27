"""observe / act end to end on a real Windows desktop (the CI runner): OCR a window's text, tap it, wait for the
result. Run with RELAYMCP_DEVICE_TESTS=1."""

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RELAYMCP_DEVICE_TESTS") != "1", reason="Windows device tests (CI)")

APP = r"""
import tkinter as t
r = t.Tk(); r.title("RelayActTest"); r.geometry("700x420+150+150"); r.configure(bg="white")
r.attributes("-topmost", True)
msg = t.StringVar(value="Waiting for input")
def pressed(): msg.set("Button pressed")
t.Button(r, text="Launch Mission", font=("Segoe UI", 28), command=pressed).pack(pady=30)
t.Label(r, textvariable=msg, font=("Segoe UI", 28), bg="white").pack(pady=10)
e = t.Entry(r, font=("Segoe UI", 28)); e.pack(pady=10)
r.after(90000, r.destroy); r.mainloop()
"""


def _text(result) -> dict:
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads([b for b in blocks if b.type == "text"][-1].text)


@pytest.fixture(scope="module")
def server():
    from relaymcp.device import focus, ocr, server
    try:
        ocr._get_engine()
    except Exception as e:
        pytest.skip(f"no Windows OCR on this machine: {e}")
    proc = subprocess.Popen([sys.executable, "-c", APP])
    deadline = time.time() + 20
    while time.time() < deadline and not focus.find_window("RelayActTest"):
        time.sleep(0.2)
    if not focus.find_window("RelayActTest"):
        proc.kill()
        pytest.skip("test window didn't appear (no interactive desktop?)")
    focus.focus_window("RelayActTest")
    time.sleep(1.0)
    yield server.build_server(18799)
    proc.kill()


def call(mcp, tool, args):
    return _text(asyncio.run(mcp.call_tool(tool, args)))


def test_observe_reads_the_window(server):
    out = call(server, "observe", {"find": "Launch Mission"})
    print("\nobserve:", out)
    assert out["text"] and "launch mission" in out["text"][0][0].lower()
    assert out["ms"] < 3000


def test_act_taps_text_and_waits_for_the_result(server):
    out = call(server, "act", {"steps": [{"focus": "RelayActTest"}, {"click_text": "Launch Mission"},
                                          {"wait_text": "Button pressed", "timeout": 8}], "observe": "text"})
    print("\nact:", out)
    assert "failed" not in out and out["done"] == "3/3", out
    assert any("button pressed" in line[0].lower() for line in out["observe"]["text"])


def test_act_types_into_a_field(server):
    label = call(server, "observe", {"find": "Button pressed"})  # the entry sits below the label
    x, y = label["text"][0][1], label["text"][0][2] + 70
    out = call(server, "act", {"steps": [{"click": [x, y]}, {"type": "hello relay"}, {"wait_text": "hello relay", "timeout": 5}]})
    print("\ntype:", out)
    assert out["done"] == "3/3", out


def test_act_reports_the_failing_step(server):
    out = call(server, "act", {"steps": [{"wait": 10}, {"tap_text": "No Such Button Anywhere"}, {"wait": 10}]})
    assert out["done"] == "1/3" and out["failed"]["step"] == 1 and "no text matching" in out["failed"]["error"]
