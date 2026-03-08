"""Resolve a capability manifest into a Nix expression."""

from __future__ import annotations

import re


def _parse_entry(entry: str) -> str:
    """Parse a single manifest entry into a Nix paths element.

    Supported forms:
    - ``python-X.Y(pkg1, pkg2)`` → python with packages
    - ``bare-tool`` → just the package name
    """
    m = re.match(r"^python-(\d+)\.(\d+)\((.+)\)$", entry.strip())
    if m:
        major, minor, pkgs_raw = m.group(1), m.group(2), m.group(3)
        attr = f"python{major}{minor}"
        pkgs = [p.strip() for p in pkgs_raw.split(",")]
        pkg_list = " ".join(f"ps.{p}" for p in pkgs)
        return f"(pkgs.{attr}.withPackages (ps: [ {pkg_list} ]))"

    # Bare tool
    return f"pkgs.{entry.strip()}"


def manifest_to_nix(manifest: list[str]) -> str:
    """Convert a list of capability manifest entries to a Nix expression.

    Returns an empty string for an empty manifest (base image only).
    """
    if not manifest:
        return ""

    paths = [_parse_entry(e) for e in manifest]
    paths_block = "\n".join(f"    {p}" for p in paths)

    return (
        "{ pkgs ? import <nixpkgs> {} }:\n"
        "pkgs.buildEnv {\n"
        '  name = "mshkn-capability";\n'
        "  paths = [\n"
        f"{paths_block}\n"
        "  ];\n"
        "}"
    )
