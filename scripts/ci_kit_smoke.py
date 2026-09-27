"""CI: build a real setup kit with throwaway settings (a fake key, a non-ASCII host label) into the given folder."""

from __future__ import annotations

import os
import sys
from pathlib import Path

out = Path(sys.argv[1]).resolve()
os.environ.setdefault("RELAYMCP_HOME", str(out.parent / "relaymcp-home"))

from relaymcp.host import config, kit  # noqa: E402

config.HOME.mkdir(parents=True, exist_ok=True)
config.KEY_FILE.write_text("unused in CI")
Path(str(config.KEY_FILE) + ".pub").write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKqCIkitSmokeTestKeyOnly relaymcp@ci\n")
cfg = config.load()
cfg["device"].update(name="ci-handheld", home_networks=["CI's Wi-Fi"], home_gateways=["00-11-22-33-44-55"])
cfg["host_label"] = "the CI runner\u2019s PC"
path = kit.build(cfg, out, "10.0.0.5", "ci-host.local")
files = sorted(p.name for p in path.iterdir())
print("kit:", path, files)
assert set(kit.KIT_FILES) <= set(files), files
