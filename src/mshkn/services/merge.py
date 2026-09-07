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


def _entry_hash(path: Path) -> str | None:
    """Identity of a path without following symlinks.

    The link target for a symlink, an md5 of the bytes for a regular file,
    None when absent. A guest rootfs is full of absolute symlinks; resolving
    one from a mounted volume lands on the host's own tree, so the merge reads
    the link, never the file it names.
    """
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


def copy_entry(src: Path, dest: Path) -> None:
    """Reproduce src at dest: a symlink as a symlink, a file as a file.

    Never writes through an existing symlink at dest, which for an absolute
    link on a mounted volume would be a write onto the host.
    """
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        dest.symlink_to(src.readlink())
    else:
        shutil.copy2(src, dest, follow_symlinks=False)


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

    for rel in sorted(all_files):
        p_file = parent / rel
        a_file = fork_a / rel
        b_file = fork_b / rel
        out_file = output / rel

        hp = _entry_hash(p_file)
        ha = _entry_hash(a_file)
        hb = _entry_hash(b_file)

        out_file.parent.mkdir(parents=True, exist_ok=True)

        if ha == hp and hb == hp:
            # Unchanged in both
            if p_file.is_symlink() or p_file.exists():
                copy_entry(p_file, out_file)
            result.unchanged += 1
        elif ha != hp and hb == hp:
            # Changed only in A
            if a_file.is_symlink() or a_file.exists():
                copy_entry(a_file, out_file)
            # else: A deleted it
            result.auto_merged += 1
        elif ha == hp and hb != hp:
            # Changed only in B
            if b_file.is_symlink() or b_file.exists():
                copy_entry(b_file, out_file)
            result.auto_merged += 1
        elif ha == hb:
            # Both changed the same way
            if a_file.is_symlink() or a_file.exists():
                copy_entry(a_file, out_file)
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
            if a_file.is_symlink() or a_file.exists():
                copy_entry(a_file, out_file)
            elif b_file.is_symlink() or b_file.exists():
                copy_entry(b_file, out_file)

    return result
