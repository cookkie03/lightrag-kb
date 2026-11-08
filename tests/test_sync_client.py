"""Check safe, deterministic local source snapshots and sync diffs."""

from sync.client import snapshot, diff


def test_snapshot_preserves_unicode_and_newlines_without_following_symlinks(tmp_path):
    (tmp_path / "note é\n.txt").write_text("hello")
    (tmp_path / "link").symlink_to(tmp_path / "note é\n.txt")
    result = snapshot(tmp_path)
    assert result["scan_complete"] is True
    assert [entry["path"] for entry in result["files"]] == ["note é\n.txt"]
    assert result["files"][0]["sha256"] == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_partial_diff_never_deletes_missing_files():
    previous = [{"path": "missing.txt", "size": 1, "mtime_ns": 1, "sha256": "x"}]
    result = diff(previous, [], complete=False)
    assert result["delete"] == []


def test_full_diff_recognizes_only_unambiguous_renames():
    old = [{"path": "old", "size": 1, "mtime_ns": 1, "sha256": "abc"}]
    new = [{"path": "new", "size": 1, "mtime_ns": 2, "sha256": "abc"}]
    result = diff(old, new, complete=True)
    assert result["rename"] == [{"old": "old", "new": "new"}]
    assert not result["add"] and not result["delete"]


def test_rename_is_not_inferred_when_another_copy_exists():
    old = [{"path": "old", "sha256": "abc"}, {"path": "existing", "sha256": "abc"}]
    new = [{"path": "new", "sha256": "abc"}, {"path": "existing", "sha256": "abc"}]
    result = diff(old, new, complete=True)
    assert result["rename"] == []
    assert result["add"] == ["new"]
    assert result["delete"] == ["old"]
