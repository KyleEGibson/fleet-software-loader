"""Append-only JSON-lines audit log, one line per device step."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    def __init__(self, path: str | Path | None, run_id: str, operator: str) -> None:
        self.path = Path(path) if path else None
        self.run_id = run_id
        self.operator = operator
        self.entries: list[dict] = []
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, device: str, step: str, ok: bool, **details) -> dict:
        entry = {
            "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": self.run_id, "operator": self.operator,
            "device": device, "step": step, "ok": ok, **details,
        }
        with self._lock:
            self.entries.append(entry)
            if self.path:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, default=str) + "\n")
        return entry
