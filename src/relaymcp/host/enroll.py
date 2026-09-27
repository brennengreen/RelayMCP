"""Enrollment: serves the setup kit on the home network (for handhelds without a USB drive) and waits for the
handheld's check-in, which reports its SSH host key, address and router. Every URL carries the kit's random token,
so other devices on the network can't fetch the kit or fake a check-in."""

from __future__ import annotations

import http.server
import threading
import urllib.parse
from pathlib import Path

from . import kit

BOOTSTRAP = """$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'
$d = Join-Path $env:TEMP 'RelayMCP-kit'; New-Item -ItemType Directory -Force -Path $d | Out-Null
foreach ($f in @({files})) {{ Invoke-WebRequest -UseBasicParsing -Uri "http://{host}/kit/{token}/$f" -OutFile (Join-Path $d $f) }}
Write-Host 'Starting RelayMCP setup (choose Yes when Windows asks)...' -ForegroundColor Cyan
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $d 'Relay-Setup.ps1')
"""


class Enrollment:
    def __init__(self, kit_dir: Path, token: str, bind: str, port: int):
        self.kit_dir, self.token, self.bind, self.port = Path(kit_dir), token, bind, port
        self.report: dict | None = None
        self.done = threading.Event()
        self.requests: list[str] = []
        self._server: http.server.ThreadingHTTPServer | None = None

    @property
    def one_liner(self) -> str:
        return f"irm http://{self.bind}:{self.port}/{self.token} | iex"

    def start(self) -> None:
        enrollment = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                url = urllib.parse.urlparse(self.path)
                parts = [p for p in url.path.split("/") if p]
                token = enrollment.token
                if parts in ([token], ["go", token]):
                    host = self.headers.get("Host") or f"{enrollment.bind}:{enrollment.port}"
                    files = ", ".join(f"'{f}'" for f in kit.KIT_FILES)
                    self._reply(BOOTSTRAP.format(files=files, host=host, token=token).encode(), "text/plain; charset=utf-8")
                    enrollment.requests.append(f"{self.client_address[0]} fetched the bootstrap script")
                elif len(parts) == 3 and parts[:2] == ["kit", token] and parts[2] in kit.KIT_FILES:
                    self._reply((enrollment.kit_dir / parts[2]).read_bytes(), "application/octet-stream")
                elif parts == ["done", token]:
                    fields = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                    fields["from"] = self.client_address[0]
                    self._reply(b"ok", "text/plain")
                    if enrollment.report is None:
                        enrollment.report = fields
                        enrollment.done.set()
                else:
                    self.send_error(404)

            def _reply(self, body: bytes, content_type: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer((self.bind, self.port), Handler)
        threading.Thread(target=self._server.serve_forever, name="enroll", daemon=True).start()

    def wait(self, timeout: float | None = None) -> dict | None:
        self.done.wait(timeout)
        return self.report

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
