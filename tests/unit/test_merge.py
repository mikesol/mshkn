from pathlib import Path

from mshkn.services.merge import three_way_merge


def test_non_overlapping_files(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    fork_a = tmp_path / "fork_a"
    fork_b = tmp_path / "fork_b"
    for d in [parent, fork_a, fork_b]:
        d.mkdir()
        (d / "shared.txt").write_text("unchanged")
    (fork_a / "a_only.txt").write_text("from a")
    (fork_b / "b_only.txt").write_text("from b")

    result = three_way_merge(parent, fork_a, fork_b)
    assert result.conflicts == []
    assert (result.merged_dir / "shared.txt").read_text() == "unchanged"
    assert (result.merged_dir / "a_only.txt").read_text() == "from a"
    assert (result.merged_dir / "b_only.txt").read_text() == "from b"


def test_conflict_both_modified(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    fork_a = tmp_path / "fork_a"
    fork_b = tmp_path / "fork_b"
    for d in [parent, fork_a, fork_b]:
        d.mkdir()
    (parent / "file.txt").write_text("original")
    (fork_a / "file.txt").write_text("version a")
    (fork_b / "file.txt").write_text("version b")

    result = three_way_merge(parent, fork_a, fork_b)
    assert len(result.conflicts) == 1
    assert result.conflicts[0].path == "file.txt"


def test_one_side_delete(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    fork_a = tmp_path / "fork_a"
    fork_b = tmp_path / "fork_b"
    for d in [parent, fork_a, fork_b]:
        d.mkdir()
    (parent / "file.txt").write_text("original")
    # fork_a deletes it, fork_b doesn't touch it
    (fork_b / "file.txt").write_text("original")

    result = three_way_merge(parent, fork_a, fork_b)
    assert result.conflicts == []
    assert not (result.merged_dir / "file.txt").exists()


def _dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    parent, a, b = tmp_path / "parent", tmp_path / "a", tmp_path / "b"
    for d in (parent, a, b):
        d.mkdir()
    return parent, a, b


def test_delete_vs_modify_is_a_conflict_resolved_toward_a(tmp_path: Path) -> None:
    parent, a, b = _dirs(tmp_path)
    (parent / "f").write_text("v0")
    (b / "f").write_text("v1")  # a deleted it, b modified it
    result = three_way_merge(parent, a, b)
    assert [c.path for c in result.conflicts] == ["f"]
    # a has nothing to take, so b's copy wins the default
    assert (result.merged_dir / "f").read_text() == "v1"


def test_both_added_different_content_is_a_conflict(tmp_path: Path) -> None:
    parent, a, b = _dirs(tmp_path)
    (a / "new").write_text("from a")
    (b / "new").write_text("from b")
    result = three_way_merge(parent, a, b)
    assert [c.path for c in result.conflicts] == ["new"]
    assert (result.merged_dir / "new").read_text() == "from a"


def test_both_added_same_content_auto_merges(tmp_path: Path) -> None:
    parent, a, b = _dirs(tmp_path)
    (a / "same").write_text("x")
    (b / "same").write_text("x")
    result = three_way_merge(parent, a, b)
    assert result.conflicts == []
    assert result.auto_merged == 1


def test_both_deleted_is_unchanged_and_absent(tmp_path: Path) -> None:
    parent, a, b = _dirs(tmp_path)
    (parent / "gone").write_text("x")
    result = three_way_merge(parent, a, b)
    assert result.conflicts == []
    assert not (result.merged_dir / "gone").exists()


def test_nested_paths_and_counts(tmp_path: Path) -> None:
    parent, a, b = _dirs(tmp_path)
    for d in (parent, a, b):
        (d / "keep").mkdir()
        (d / "keep" / "same.txt").write_text("same")
    (a / "keep" / "a.txt").write_text("a")
    result = three_way_merge(parent, a, b, output=tmp_path / "out")
    assert result.merged_dir == tmp_path / "out"
    assert (result.unchanged, result.auto_merged) == (1, 1)
    assert (result.merged_dir / "keep" / "a.txt").read_text() == "a"
