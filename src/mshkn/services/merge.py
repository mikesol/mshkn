from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ConflictInfo:
    path: str
    parent_hash: str | None
    fork_a_hash: str | None
    fork_b_hash: str | None


@dataclass
class MergeResult:
    merged_dir: Path
    conflicts: list[ConflictInfo] = field(default_factory=list)
    auto_merged: int = 0
    unchanged: int = 0


def _entry_hash(path: Path | None) -> str | None:
    """Identity of a path without following symlinks.

    The link target for a symlink, an md5 of the bytes for a regular file,
    None when absent. A guest rootfs is full of absolute symlinks; resolving
    one from a mounted volume lands on the host's own tree, so the merge reads
    the link, never the file it names.
    """
    if path is None:
        return None
    if path.is_symlink():
        return "link:" + str(path.readlink())
    if path.is_file():
        return hashlib.md5(path.read_bytes()).hexdigest()
    return None


def all_relative_entries(*dirs: Path) -> set[str]:
    """Regular files and symlinks (never followed) below each directory.

    A symlink to a directory is an entry in its own right, not a directory to
    descend into: descending would walk out of the volume and onto the host.
    """
    entries: set[str] = set()
    for d in dirs:
        if not d.is_dir():
            continue
        for root, dirnames, filenames in os.walk(d, followlinks=False):
            root_path = Path(root)
            for name in list(dirnames):
                if (root_path / name).is_symlink():
                    dirnames.remove(name)
                    entries.add(str((root_path / name).relative_to(d)))
            for name in filenames:
                f = root_path / name
                if f.is_symlink() or f.is_file():
                    entries.add(str(f.relative_to(d)))
    return entries


def entry_path(root: Path, rel: str) -> Path | None:
    """`root / rel`, or None when a symlink stands anywhere above it.

    A symlink is an entry, never a door. If `usr/sbin` is a link in one tree
    then `usr/sbin/init` is not reachable in that tree at all: reaching it
    would leave the volume and land on the host's own filesystem. The caller
    reads that None as "absent here", so a fork that replaced a directory with
    a link deletes the directory's children and adds the link.
    """
    walked = root
    for part in Path(rel).parts[:-1]:
        walked = walked / part
        if walked.is_symlink():
            return None
    return root / rel


def unlink_stale_ancestors(output: Path, entries: set[str]) -> None:
    """Remove symlinks in `output` that stand above an entry of the result.

    An output volume snapped from the parent carries the parent's symlinks. A
    fork that replaced one of them with a real directory makes that link stale:
    left in place it shadows the children about to be copied, and the delete
    pass then removes the link too, so neither survives.
    """
    for rel in sorted(entries):
        walked = output
        for part in Path(rel).parts[:-1]:
            walked = walked / part
            if walked.is_symlink() and str(walked.relative_to(output)) not in entries:
                walked.unlink()


def copy_entry(src: Path, dest: Path) -> None:
    """Reproduce src at dest: a symlink as a symlink, a file as a file.

    Never writes through an existing symlink at dest, which for an absolute
    link on a mounted volume would be a write onto the host. A real directory
    at dest is removed: the output volume is snapped from the parent, so it
    carries the parent's directories, and a fork may have replaced one of them
    with a link.
    """
    if dest.is_symlink():
        dest.unlink()
    elif dest.is_dir():
        shutil.rmtree(dest)
    elif dest.exists():
        dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        dest.symlink_to(src.readlink())
    else:
        shutil.copy2(src, dest, follow_symlinks=False)


def _apply(src: Path | None, src_hash: str | None, dest: Path | None) -> None:
    """Copy src to dest when both are entries no symlink stands above.

    A None src is a delete, a None hash is a path that is not a file or a link
    (a directory another tree replaced with one), and a None dest means a link
    already won that path in the output.
    """
    if src is None or src_hash is None or dest is None:
        return
    copy_entry(src, dest)


def three_way_merge(
    parent: Path,
    fork_a: Path,
    fork_b: Path,
    output: Path | None = None,
) -> MergeResult:
    if output is None:
        output = parent.parent / "merged"
    output.mkdir(parents=True, exist_ok=True)

    result = MergeResult(merged_dir=output)
    all_files = all_relative_entries(parent, fork_a, fork_b)

    # Sorted, so a link lands in the output before the children of the
    # directory it replaced are considered, and `entry_path` sees it there.
    for rel in sorted(all_files):
        p_file = entry_path(parent, rel)
        a_file = entry_path(fork_a, rel)
        b_file = entry_path(fork_b, rel)
        out_file = entry_path(output, rel)

        hp = _entry_hash(p_file)
        ha = _entry_hash(a_file)
        hb = _entry_hash(b_file)

        if ha == hp and hb == hp:
            # Unchanged in both
            _apply(p_file, hp, out_file)
            result.unchanged += 1
        elif ha != hp and hb == hp:
            # Changed only in A; a None hash there is A deleting it
            _apply(a_file, ha, out_file)
            result.auto_merged += 1
        elif ha == hp and hb != hp:
            # Changed only in B
            _apply(b_file, hb, out_file)
            result.auto_merged += 1
        elif ha == hb:
            # Both changed the same way
            _apply(a_file, ha, out_file)
            result.auto_merged += 1
        else:
            # Conflict
            result.conflicts.append(
                ConflictInfo(
                    path=rel,
                    parent_hash=hp,
                    fork_a_hash=ha,
                    fork_b_hash=hb,
                )
            )
            # Default: take fork_a
            if ha is not None:
                _apply(a_file, ha, out_file)
            else:
                _apply(b_file, hb, out_file)

    return result
