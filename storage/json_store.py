"""Persist versioned catalog snapshots safely across processes."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path


class JsonStore:
    """Serialize catalog transactions using a stable lock and atomic replacement."""

    def __init__(self, path: Path | str):
        """Keep the snapshot and its lock in the same private KB directory."""
        self.path = Path(path)
        self._lock_file = None
        self._data = None

    def __enter__(self):
        """Open lock, load snapshot, validate, and yield data for transaction."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._lock_file = lock_path.open("a+")
        fcntl.flock(self._lock_file, fcntl.LOCK_EX)

        if self.path.exists():
            try:
                with self.path.open(encoding="utf-8") as stream:
                    self._data = json.load(stream)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self.__exit__(type(exc), exc, None)
                raise ValueError("invalid catalog snapshot") from exc
            if not isinstance(self._data, dict) or self._data.get("version") != 1 or any(
                not isinstance(self._data.get(key), dict)
                or any(not isinstance(record, dict) for record in self._data[key].values())
                for key in ("sources", "files", "documents", "jobs")
            ):
                self.__exit__(None, None, None)
                raise ValueError("invalid or unsupported catalog snapshot")
        else:
            self._data = {"version": 1, "sources": {}, "files": {}, "documents": {}, "jobs": {}}
        return self._data

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Commit on success; rollback on exception by not saving."""
        if exc_type is None and self._data is not None:
            self._save(self._data)
        if self._lock_file is not None:
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            finally:
                self._lock_file.close()
                self._lock_file = None
        self._data = None
        return False

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