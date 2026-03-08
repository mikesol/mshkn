from pathlib import Path

from mshkn.checkpoint.merge import three_way_merge


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
