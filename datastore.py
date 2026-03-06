from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from threading import Lock
from typing import Any


@dataclass
class JsonStore:
    path: str
    _lock: Lock = Lock()

    def _ensure_dir(self) -> None:
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)

    def read(self) -> dict[str, Any]:
        self._ensure_dir()
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except FileNotFoundError:
                return {}
            except json.JSONDecodeError:
                # se corrompeu, renomeia para debug e recomeça limpo
                try:
                    os.rename(self.path, self.path + ".corrupted")
                except Exception:
                    pass
                return {}

    def write(self, data: dict[str, Any]) -> None:
        self._ensure_dir()
        with self._lock:
            # escrita atômica (evita corromper se o bot cair no meio)
            fd, tmp = tempfile.mkstemp(prefix="tmp_", suffix=".json", dir=os.path.dirname(self.path) or ".")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            finally:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    pass
