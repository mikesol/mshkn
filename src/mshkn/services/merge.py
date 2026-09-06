from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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


def _file_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.md5(path.read_bytes()).hexdigest()


def _all_relative_files(*dirs: Path) -> set[str]:
    files: set[str] = set()
    for d in dirs:
        if d.exists():
            for f in d.rglob("*"):
                if f.is_file():
                    files.add(str(f.relative_to(d)))
    return files


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
    all_files = _all_relative_files(parent, fork_a, fork_b)

    for rel in sorted(all_files):
        p_file = parent / rel
        a_file = fork_a / rel
        b_file = fork_b / rel
        out_file = output / rel

        hp = _file_hash(p_file)
        ha = _file_hash(a_file)
        hb = _file_hash(b_file)

        out_file.parent.mkdir(parents=True, exist_ok=True)

        if ha == hp and hb == hp:
            # Unchanged in both
            if p_file.exists():
                shutil.copy2(p_file, out_file)
            result.unchanged += 1
        elif ha != hp and hb == hp:
            # Changed only in A
            if a_file.exists():
                shutil.copy2(a_file, out_file)
            # else: A deleted it
            result.auto_merged += 1
        elif ha == hp and hb != hp:
            # Changed only in B
            if b_file.exists():
                shutil.copy2(b_file, out_file)
            result.auto_merged += 1
        elif ha == hb:
            # Both changed the same way
            if a_file.exists():
                shutil.copy2(a_file, out_file)
            result.auto_merged += 1
        elif hp is None and ha is not None and hb is None:
            # Added only in A
            shutil.copy2(a_file, out_file)
            result.auto_merged += 1
        elif hp is None and ha is None and hb is not None:
            # Added only in B
            shutil.copy2(b_file, out_file)
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
            if a_file.exists():
                shutil.copy2(a_file, out_file)
            elif b_file.exists():
                shutil.copy2(b_file, out_file)

    return result
