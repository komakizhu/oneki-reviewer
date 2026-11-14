from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


EMPTY_STATE: dict[str, Any] = {
    "version": 1,
    "daily": {},
    "batches": {},
    "timing_history": [],
}


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return json.loads(json.dumps(EMPTY_STATE))
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return json.loads(json.dumps(EMPTY_STATE))
        if not isinstance(value, dict):
            return json.loads(json.dumps(EMPTY_STATE))
        value.setdefault("version", 1)
        value.setdefault("daily", {})
        value.setdefault("batches", {})
        value.setdefault("timing_history", [])
        value.pop("auto_hard_streaks", None)
        return value

    def save(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

