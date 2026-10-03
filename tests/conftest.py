import importlib
import os
import select
import sys
import threading
import time

# One BLAS thread per test process, set before numpy loads. The camera simulator renders each frame with one big
# matrix product (129600 x 3 by 3 x 3); OpenBLAS split it over every core, so each frame waited for all of them. On
# the 4-vCPU Linux runners that made rendering slower and jittery: p50 12-13 ms, p99 14-23, worst 18-44 with the
# default threads (1,500 frames, twice); 10, 11 and 12 with one.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_var, "1")

import pytest  # noqa: E402


def _precise_timers() -> bool:
    """GitHub's macOS runners run every job under a utility QoS clamp (`sudo taskinfo <pid>`: "eff qos clamp:
    THREAD_QOS_UTILITY", "eff latency qos: LATENCY_QOS_TIER_3"), and macOS coalesces such a process's timers:
    time.sleep(0.001) returns ~8 ms late, time.sleep(0.02) 50-150 ms late, and timed Event waits the same (on a
    developer's Mac: up to 25-50% late, at most 5-10 ms). The real-time tests (simulated cameras, telemetry and game
    loops; the behaviors' 20-120 Hz tick loops; the camera servo) can't run on that clock, and the handheld (Windows,
    a 1 ms timer) never runs on one. kqueue timers marked NOTE_CRITICAL are exempt from coalescing (~0.1 ms late
    under the same clamp), so on macOS the tests' sleeps and Event.wait timeouts use them. Everything still runs in
    real time and is descheduled like any process; only the coalescing is gone. Reproduce the runner's clock locally
    with `taskpolicy -c utility pytest ...`."""
    if sys.platform != "darwin" or not hasattr(select, "kqueue"):
        return False
    NOTE_NSECONDS, NOTE_CRITICAL = 0x4, 0x20  # <sys/event.h>
    plain_sleep, plain_wait, clock = time.sleep, threading.Event.wait, time.monotonic
    local = threading.local()

    def kqueue():
        if getattr(local, "pid", None) != os.getpid():  # one per thread (a forked child has none of its parent's)
            local.kq, local.pid = select.kqueue(), os.getpid()
        return local.kq

    def arm(seconds):
        """Wait on a coalescing-exempt timer; False if the kernel refused it."""
        timer = select.kevent(1, select.KQ_FILTER_TIMER, select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                              NOTE_NSECONDS | NOTE_CRITICAL, max(1, int(seconds * 1e9)))
        fired = kqueue().control([timer], 1, None)
        return bool(fired) and not fired[0].flags & select.KQ_EV_ERROR

    def sleep(seconds):
        if seconds < 0:
            raise ValueError("sleep length must be non-negative")
        if seconds == 0:
            return plain_sleep(0)
        end = clock() + seconds
        left = float(seconds)
        while left > 0:
            if not arm(left):
                return plain_sleep(max(0.0, end - clock()))
            left = end - clock()

    def wait(self, timeout=None):
        if timeout is None:
            return plain_wait(self)
        end = clock() + timeout
        if timeout > 0.25 and plain_wait(self, timeout - 0.25):  # the bulk of a long wait as before (a set() ends
            return True                                             # it at once; coalescing adds at most ~0.16 s)
        while not self.is_set():
            left = end - clock()
            if left <= 0:
                return False
            sleep(min(left, 0.001))
        return True

    try:
        if not arm(0.0005):
            return False
    except OSError:
        return False
    time.sleep, threading.Event.wait = sleep, wait
    return True


PRECISE_TIMERS = _precise_timers()


def pytest_collection_modifyitems(config, items):
    """Tests marked `timing` steer the simulated camera through its visual odometry in real time. A runner that stalls
    the process for 40-70 ms at the wrong moment (the macOS and Windows VMs do, even with precise timers) delivers a
    frame the odometry can't follow, and the turn or calibration under test reads it wrong: 8 of ~1,100 runs of them
    in two 5x stress runs of the four test jobs. They get two more tries (pytest-rerunfailures); a real regression
    fails all three, and every retry is reported: "N rerun" in the summary line, and the test in the short summary."""
    if not config.pluginmanager.hasplugin("rerunfailures"):
        return
    for item in items:
        if item.get_closest_marker("timing") and not item.get_closest_marker("flaky"):
            item.add_marker(pytest.mark.flaky(reruns=2))


@pytest.fixture(autouse=True)
def _no_dev_mode(monkeypatch):
    """Tests start outside dev mode even when run from a dev shell (scripts/dev-env.sh); dev-mode tests opt in."""
    monkeypatch.delenv("RELAYMCP_DEV", raising=False)
    monkeypatch.delenv("RELAYMCP_DEV_ALLOW", raising=False)


@pytest.fixture(autouse=True)
def _no_real_handheld(tmp_path, monkeypatch):
    """No test may reach a real handheld: ssh reads ~/.ssh/config from the account (not $HOME), so a developer's
    RelayMCP host entry would otherwise work from any test. Every ssh/scp RelayMCP starts is a missing program."""
    monkeypatch.setenv("RELAYMCP_SSH", str(tmp_path / "ssh-is-disabled-in-tests"))


@pytest.fixture()
def relay_home(tmp_path, monkeypatch):
    """A throwaway ~/.relaymcp (and HOME) so tests never touch the real one."""
    monkeypatch.setenv("RELAYMCP_HOME", str(tmp_path / "relaymcp"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))  # Path.home() on Windows
    (tmp_path / "home").mkdir()
    for name in [m for m in sys.modules if m.startswith("relaymcp.host")]:
        del sys.modules[name]
    from relaymcp.host import config
    importlib.reload(config)
    return tmp_path
