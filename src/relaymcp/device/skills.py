"""A skill library: programs that worked, saved by name per app, so a model calls them instead of re-sending (and
re-debugging) code, the way Voyager keeps verified Minecraft skills. Each skill records how its runs went, and its
inputs (names the code uses without defining them, e.g. TX, TY) are worked out when it's saved."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

COMMON = "common"  # skills for every app
NAME = re.compile(r"^[a-z0-9_][a-z0-9_-]{0,40}$")


def app_key(app: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (app or "").lower().removesuffix(".exe")).strip("-") or "default"


class SkillStore:
    def __init__(self, folder: Path):
        self.folder = Path(folder)

    @staticmethod
    def check_name(name: str) -> str:
        name = (name or "").strip().lower()
        if not NAME.match(name):
            raise ValueError("a skill name is 1-41 letters, digits, _ or - (e.g. chop_tree)")
        return name

    def _path(self, app: str, name: str) -> Path:
        return self.folder / app_key(app) / f"{name}.json"

    def save(self, app: str, name: str, code: str, description: str, inputs: list[str], common: bool = False) -> dict:
        name = self.check_name(name)
        if not description.strip():
            raise ValueError("describe the skill in a sentence (what it does, what it needs): it's how it's found later")
        where = COMMON if common else app
        path = self._path(where, name)
        old = self._read(path) or {}
        skill = {"name": name, "description": description.strip()[:300], "code": code, "inputs": inputs,
                 "app": where, "saved": time.strftime("%Y-%m-%d %H:%M"),
                 "runs": 0 if old.get("code") != code else old.get("runs", 0),
                 "ok": 0 if old.get("code") != code else old.get("ok", 0)}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(skill), encoding="utf-8")
        return skill

    def _read(self, path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def get(self, app: str, name: str) -> dict | None:
        name = self.check_name(name)
        return self._read(self._path(app, name)) or self._read(self._path(COMMON, name))

    def list(self, app: str) -> list[dict]:
        out, seen = [], set()
        for where in (app, COMMON):
            folder = self.folder / app_key(where)
            for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
                s = self._read(path)
                if not s or s["name"] in seen:
                    continue
                seen.add(s["name"])
                entry = {"name": s["name"], "description": s["description"], "runs": s.get("runs", 0),
                         "ok": s.get("ok", 0)}
                if s.get("inputs"):
                    entry["inputs"] = s["inputs"]
                if where == COMMON:
                    entry["common"] = True
                if s.get("last"):
                    entry["last"] = s["last"]
                out.append(entry)
        return out

    def forget(self, app: str, name: str) -> bool:
        name = self.check_name(name)
        for where in (app, COMMON):
            path = self._path(where, name)
            if path.exists():
                path.unlink()
                return True
        return False

    def record(self, app: str, name: str, outcome: str) -> None:
        """After a run: "done", or why it didn't finish."""
        for where in (app, COMMON):
            path = self._path(where, name)
            s = self._read(path)
            if s:
                s["runs"] = s.get("runs", 0) + 1
                s["ok"] = s.get("ok", 0) + (outcome == "done")
                s["last"] = outcome[:120]
                try:
                    path.write_text(json.dumps(s), encoding="utf-8")
                except OSError:
                    pass
                return
