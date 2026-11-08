"""Persist versioned catalog snapshots safely across processes."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


class JsonStore:
    """Serialize catalog transactions using a stable lock and atomic replacement."""

    def __init__(self, path: Path | str):
        """Keep the snapshot and its lock in the same private KB directory."""
        self.path = Path(path)

    @contextmanager
    def transaction(self):
        """Commit only on success; reject corrupt or unknown snapshots unchanged."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(self.path.suffix + ".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.path.exists():
                try:
                    with self.path.open(encoding="utf-8") as stream:
                        data = json.load(stream)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise ValueError("invalid catalog snapshot") from exc
                if not isinstance(data, dict) or data.get("version") != 1 or any(
                    not isinstance(data.get(key), dict)
                    or any(not isinstance(record, dict) for record in data[key].values())
                    for key in ("sources", "files", "documents", "jobs")
                ):
                    raise ValueError("invalid or unsupported catalog snapshot")
            else:
                data = {"version": 1, "sources": {}, "files": {}, "documents": {}, "jobs": {}}
            yield data
            self._save(data)

    def _save(self, data):
        """Fsync a same-directory temporary file before replacing the snapshot."""
        fd, name = tempfile.mkstemp(prefix=".catalog-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)
