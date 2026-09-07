from pathlib import Path

from mshkn.services.merge import all_relative_entries, three_way_merge


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


def test_delete_vs_modify_is_a_conflict_that_falls_back_to_b_when_a_deleted(
    tmp_path: Path,
) -> None:
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


def test_both_deleted_is_absent_and_counted_as_auto_merged(tmp_path: Path) -> None:
    """Both sides deleting a file is not "unchanged": it lands on "changed the same way"."""
    parent, a, b = _dirs(tmp_path)
    (parent / "gone").write_text("x")
    result = three_way_merge(parent, a, b)
    assert result.conflicts == []
    assert not (result.merged_dir / "gone").exists()
    assert (result.unchanged, result.auto_merged) == (0, 1)


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


def test_b_deletes_and_a_leaves_alone_removes_the_file(tmp_path: Path) -> None:
    """B deleting a file A did not touch is an auto-merge, and the file is gone.

    This is the "changed only in B" arm with `b_file` absent: nothing is copied
    to the output, so the delete propagates rather than being silently undone.
    """
    parent, a, b = _dirs(tmp_path)
    (parent / "doomed").write_text("x")
    (a / "doomed").write_text("x")
    result = three_way_merge(parent, a, b)
    assert result.conflicts == []
    assert not (result.merged_dir / "doomed").exists()
    assert (result.unchanged, result.auto_merged) == (0, 1)


def test_a_missing_fork_directory_contributes_no_files(tmp_path: Path) -> None:
    """`all_relative_entries` skips a directory that does not exist.

    A fork volume that was never mounted must not make the merge raise; the
    other two sides still merge.
    """
    parent, a, _b = _dirs(tmp_path)
    (parent / "kept").write_text("x")
    (a / "kept").write_text("x")
    result = three_way_merge(parent, a, tmp_path / "never-mounted")
    assert result.conflicts == []
    assert not (result.merged_dir / "kept").exists(), "the absent side reads as a delete"
    assert (result.unchanged, result.auto_merged) == (0, 1)


def test_symlinks_are_merged_as_links_and_never_followed(tmp_path: Path) -> None:
    """A symlink is an entry whose identity is its target string.

    The exported `mshkn-base` filesystem has 159 absolute symlinks; following
    one from a mounted volume lands on the host's own tree, which the merge
    then hashed, copied and wrote back through.
    """
    host_file = tmp_path / "host-file"
    host_file.write_text("host bytes")
    before = host_file.stat().st_mtime_ns
    parent, fork_a, fork_b = (tmp_path / n for n in ("parent", "fork_a", "fork_b"))
    for d in (parent, fork_a, fork_b):
        d.mkdir()
        (d / "init").symlink_to(host_file)  # absolute link, like /usr/sbin/init
        (d / "escape").symlink_to(tmp_path)  # absolute link to a directory
    (fork_a / "init").unlink()
    (fork_a / "init").symlink_to("/lib/systemd/systemd-a")  # changed only in A

    result = three_way_merge(parent, fork_a, fork_b)
    out = result.merged_dir
    assert (out / "init").is_symlink()
    assert str((out / "init").readlink()) == "/lib/systemd/systemd-a"
    assert (out / "escape").is_symlink() and (out / "escape").readlink() == tmp_path
    # the symlinked directory was not descended into, so nothing under it was copied
    assert all_relative_entries(parent) == {"init", "escape"}
    assert sorted(p.name for p in out.iterdir()) == ["escape", "init"]
    assert result.conflicts == [] and result.auto_merged == 1 and result.unchanged == 1
    assert host_file.read_text() == "host bytes" and host_file.stat().st_mtime_ns == before


def test_a_file_replaced_by_a_symlink_in_both_forks_conflicts(tmp_path: Path) -> None:
    """A regular file on one side and a symlink on the other is different content."""
    parent, fork_a, fork_b = _dirs(tmp_path)
    (parent / "f").write_text("data")
    (fork_a / "f").symlink_to("/a")
    (fork_b / "f").symlink_to("/b")
    result = three_way_merge(parent, fork_a, fork_b)
    assert [c.path for c in result.conflicts] == ["f"]
    assert str((result.merged_dir / "f").readlink()) == "/a"


def _hostroot(tmp_path: Path) -> tuple[Path, int]:
    """A stand-in for the host's own tree, with one file to protect."""
    hostroot = tmp_path / "HOSTROOT"
    hostroot.mkdir()
    (hostroot / "init").write_bytes(b"host init")
    return hostroot, (hostroot / "init").stat().st_mtime_ns


def test_a_directory_replaced_by_a_symlink_is_never_written_through(tmp_path: Path) -> None:
    """`usr/sbin` as a link in one tree makes `usr/sbin/init` unreachable in it.

    Entries are processed in sorted order, so the link lands in the output
    before its former children are considered. Reaching a child through it
    would read and write the host's tree.
    """
    hostroot, before = _hostroot(tmp_path)
    parent, fork_a, fork_b = _dirs(tmp_path)
    for d, content in ((parent, b"guest init"), (fork_a, b"A init")):
        (d / "usr" / "sbin").mkdir(parents=True)
        (d / "usr" / "sbin" / "init").write_bytes(content)
    (fork_b / "usr").mkdir()
    (fork_b / "usr" / "sbin").symlink_to(hostroot)

    result = three_way_merge(parent, fork_a, fork_b)
    out = result.merged_dir
    assert (hostroot / "init").read_bytes() == b"host init"
    assert (hostroot / "init").stat().st_mtime_ns == before
    assert sorted(p.name for p in hostroot.iterdir()) == ["init"]
    assert (out / "usr" / "sbin").is_symlink()
    assert (out / "usr" / "sbin").readlink() == hostroot
    # A modified a file whose directory B replaced with a link: a conflict the
    # link wins, so nothing is written under it.
    assert [c.path for c in result.conflicts] == ["usr/sbin/init"]


def test_the_mirror_case_a_holds_the_link_and_the_host_is_still_untouched(
    tmp_path: Path,
) -> None:
    """The same shape with the link in fork A: no read, no unlink, no exception."""
    hostroot, before = _hostroot(tmp_path)
    parent, fork_a, fork_b = _dirs(tmp_path)
    for d, content in ((parent, b"guest init"), (fork_b, b"B init")):
        (d / "usr" / "sbin").mkdir(parents=True)
        (d / "usr" / "sbin" / "init").write_bytes(content)
    (fork_a / "usr").mkdir()
    (fork_a / "usr" / "sbin").symlink_to(hostroot)

    result = three_way_merge(parent, fork_a, fork_b)
    out = result.merged_dir
    assert (hostroot / "init").read_bytes() == b"host init"
    assert (hostroot / "init").stat().st_mtime_ns == before
    assert sorted(p.name for p in hostroot.iterdir()) == ["init"]
    assert (out / "usr" / "sbin").is_symlink()
    assert (out / "usr" / "sbin").readlink() == hostroot
    assert [c.path for c in result.conflicts] == ["usr/sbin/init"]
