"""WMI over COM (used for brightness instead of starting PowerShell). Run with RELAYMCP_DEVICE_TESTS=1."""

import os
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RELAYMCP_DEVICE_TESTS") != "1", reason="Windows device tests (CI)")


def test_wmi_over_com_is_fast():
    from relaymcp.device import system
    t0 = time.monotonic()
    os_obj = system.wmi_first("SELECT Caption FROM Win32_OperatingSystem", "root\\CIMV2")
    assert os_obj is not None and "Windows" in str(os_obj.Caption)
    assert time.monotonic() - t0 < 2.0  # first call loads COM; later calls are ~10-50 ms
    t1 = time.monotonic()
    system.wmi_first("SELECT Caption FROM Win32_OperatingSystem", "root\\CIMV2")
    assert time.monotonic() - t1 < 0.5


def test_brightness_never_raises_without_a_panel():
    from relaymcp.device import system
    r = system.brightness()  # VMs have no internal panel: None, not an exception
    assert "brightness_percent" in r
