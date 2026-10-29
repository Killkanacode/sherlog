"""Persistent platform state (deploy history, config, flags, change events).

Stored as one JSON document; the platform is a single process so a lock and
atomic file replace are enough.
"""

import json
import os
import threading
from pathlib import Path

MAX_EVENTS = 1000


class Store:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        if self._path.exists():
            self.data = json.loads(self._path.read_text())
        else:
            self.data = {"services": {}, "flags": {}, "events": []}

    def save(self) -> None:
        with self._lock:
            self.data["events"] = self.data["events"][-MAX_EVENTS:]
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            os.replace(tmp, self._path)

    @property
    def services(self) -> dict:
        return self.data["services"]

    @property
    def flags(self) -> dict:
        return self.data["flags"]

    @property
    def events(self) -> list:
        return self.data["events"]
