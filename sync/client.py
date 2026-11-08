"""Build safe local source snapshots and compute transfer deltas."""

import hashlib
import os
from collections import Counter
from pathlib import Path


def snapshot(folder):
    """Enumerate regular files without following symlinks and report scan errors."""
    root = Path(folder)
    errors, files = [], []
    if not root.is_dir() or root.is_symlink():
        return {"files": [], "scan_complete": False, "scan_errors": ["source folder unavailable"]}

    def onerror(error):
        """Make enumeration failures visible rather than implying deletions."""
        errors.append(str(error))

    for directory, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in names:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                before = path.stat()
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    raise OSError("file changed during scan")
                files.append({"path": path.relative_to(root).as_posix(), "size": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": digest.hexdigest()})
            except OSError as error:
                errors.append(f"{path}: {error}")
    return {"files": sorted(files, key=lambda f: f["path"]), "scan_complete": not errors, "scan_errors": errors}


def diff(previous, current, complete):
    """Plan file changes; infer renames only for unique hashes in a complete scan."""
    old = {item["path"]: item for item in previous}
    new = {item["path"]: item for item in current}
    removed = set(old) - set(new) if complete else set()
    added = set(new) - set(old)
    modified = sorted(path for path in set(old) & set(new) if old[path].get("sha256") != new[path].get("sha256"))
    old_counts = Counter(item.get("sha256") for item in old.values())
    new_counts = Counter(item.get("sha256") for item in new.values())
    renames = []
    for old_path in sorted(removed):
        digest = old[old_path].get("sha256")
        if digest and old_counts[digest] == new_counts[digest] == 1:
            new_path = next(path for path in added if new[path].get("sha256") == digest)
            renames.append({"old": old_path, "new": new_path})
    removed.difference_update(item["old"] for item in renames)
    added.difference_update(item["new"] for item in renames)
    return {"add": sorted(added), "modify": modified, "rename": renames, "delete": sorted(removed), "unchanged": sorted(set(old) & set(new) - set(modified))}
