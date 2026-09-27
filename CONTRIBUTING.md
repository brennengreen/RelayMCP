# Contributing

Thanks for helping! RelayMCP is small and aims to stay that way: dependable, easy to set up, and safe by default.

## Getting set up

```sh
git clone https://github.com/brennengreen/RelayMCP && cd RelayMCP
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest && .venv/bin/ruff check src tests scripts
```

With a handheld, `uv tool install -e .` makes your checkout the real `relaymcp` command, and `relaymcp deploy` pushes
device-side changes to the handheld in about 30 seconds. See [docs/development.md](docs/development.md).

## Guidelines

- **Host code** (`src/relaymcp/host`) must run on the Python 3.9 standard library alone, on macOS, Linux and Windows.
- **Device code** (`src/relaymcp/device`) targets Windows 11 and Python 3.12. It must never show a window or console,
  steal focus, or keep the handheld awake when nobody is using it, and it must stay off away from home.
- **No antivirus-unfriendly tricks:** no `conhost --headless`, no `.cmd`/script restart loops, no obfuscated
  commands, no Defender exclusions.
- **Security-relevant changes** (anything that listens, authenticates, or widens what a voice prompt can do) need a
  note in the PR description and an update to [docs/security.md](docs/security.md).
- Add or update tests for host-side behavior. For device changes, describe how you tested on real hardware (device
  model and Windows version).
- Keep user-facing text plain and friendly: people read it on a 7-inch screen.

## Reporting bugs

Please include `relaymcp --version`, your OS, the handheld model, `relaymcp doctor` output, and the relevant lines
from `relaymcp logs` and `relaymcp logs --device`. Remove your IP addresses and names if you prefer.
