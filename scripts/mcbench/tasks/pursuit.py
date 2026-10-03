# Bench task "pursuit": keep facing a direction that sweeps round at RATE degrees/s (pitch fixed at PITCH) for DUR
# seconds. Each telemetry sample is compared with where the target was at that sample's own game tick (it arrives
# latency_ms after the tick and is read age_ms after arriving; a tick's angles trail the picture by ANGLE_LAG,
# ~45 ms on the handheld), after ACQUIRE seconds to catch up. bias = steady lag or lead; jitter = the rest.
# Inputs: RATE, PITCH, DUR, ACQUIRE, ANGLE_LAG.


def wrap(a):
    return (a + 180.0) % 360.0 - 180.0


s = telemetry()
y0 = s["yaw"] + 15.0
face(y0, PITCH, timeout=3.0, confirm=True)
t0 = elapsed()
keep_facing(lambda: (y0 + RATE * (elapsed() - t0), PITCH))
errs, perrs, last = [], [], None
while elapsed() - t0 < DUR:
    s = until(lambda: (telemetry() or {}).get("tick") not in (None, last) and telemetry(), timeout=0.3, hz=120)
    if not s:
        continue
    last = s["tick"]
    at = elapsed() - t0 - (float(s.get("age_ms") or 0.0) + float(s.get("latency_ms") or 0.0)) / 1000.0 - ANGLE_LAG
    if at >= ACQUIRE:
        errs.append(wrap(s["yaw"] - (y0 + RATE * at)))
        perrs.append(s["pitch"] - PITCH)
stop_facing()
n = len(errs)
mean = sum(errs) / n if n else 0.0
result = {"rate": RATE, "samples": n,
          "rms": round(math.sqrt(sum(e * e for e in errs) / n), 2) if n else None,
          "bias": round(mean, 2) if n else None,
          "jitter": round(math.sqrt(sum((e - mean) ** 2 for e in errs) / n), 2) if n else None,
          "max": round(max(abs(e) for e in errs), 2) if n else None,
          "pitch_rms": round(math.sqrt(sum(e * e for e in perrs) / n), 2) if n else None}
