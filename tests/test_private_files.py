"""Tests for write_private_file: owner-only, atomic, never follows a planted path."""

import os

import pytest

from src.utils import private_files
from src.utils.private_files import write_private_file


def test_new_file_is_0600_and_created_dirs_are_0700(tmp_path):
    target = tmp_path / "a" / "b" / "state.json"
    write_private_file(target, "data")
    assert target.read_text() == "data"
    assert (target.stat().st_mode & 0o777) == 0o600
    assert (target.parent.stat().st_mode & 0o777) == 0o700
    assert (target.parent.parent.stat().st_mode & 0o777) == 0o700


def test_existing_parent_dir_keeps_its_mode(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o755)
    write_private_file(parent / "state.json", "data")
    assert (parent.stat().st_mode & 0o777) == 0o755


def test_replaces_existing_file_and_tightens_it(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("old")
    target.chmod(0o644)
    write_private_file(target, "new")
    assert target.read_text() == "new"
    assert (target.stat().st_mode & 0o777) == 0o600


def test_leaves_no_temp_file_behind(tmp_path):
    target = tmp_path / "state.json"
    write_private_file(target, "data")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]


def test_planted_symlink_at_old_temp_path_is_not_followed(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    target = tmp_path / "state.json"
    # The old code wrote through a fixed "<name>.tmp" without O_EXCL.
    (tmp_path / "state.json.tmp").symlink_to(victim)

    write_private_file(target, "secret")

    assert victim.read_text() == "untouched"
    assert target.read_text() == "secret"
    assert not target.is_symlink()


def test_temp_name_is_unique_per_write(tmp_path, monkeypatch):
    names = []
    real_replace = os.replace

    def recording_replace(src, dst):
        names.append(os.path.basename(src))
        real_replace(src, dst)

    monkeypatch.setattr(private_files.os, "replace", recording_replace)
    target = tmp_path / "state.json"
    write_private_file(target, "one")
    write_private_file(target, "two")
    assert len(set(names)) == 2
    assert all(n.startswith("state.json.") for n in names)


def test_failed_write_removes_temp_and_keeps_old_file(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text("old")

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(private_files.os, "replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        write_private_file(target, "new")
    assert target.read_text() == "old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]
