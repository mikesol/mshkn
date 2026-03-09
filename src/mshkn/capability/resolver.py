"""Resolve a capability manifest into a Nix expression."""

from __future__ import annotations

import re

# Aliases for bare tool names that don't map directly to nixpkgs attr names.
_BARE_ALIASES: dict[str, str] = {
    "python": "python3",
    "node": "nodejs",
}


def _versioned_attr(tool: str, version: str) -> str:
    """Build a nixpkgs attribute name for a tool@version specifier."""
    if tool == "python":
        # python@3.11 → python311
        return f"python{version.replace('.', '')}"
    if tool == "node":
        # node@22 → nodejs_22
        return f"nodejs_{version}"
    # Generic fallback: tool@ver → tool_ver
    return f"{tool}_{version}"


def _parse_entry(entry: str) -> str:
    """Parse a single manifest entry into a Nix paths element.

    Supported forms:
    - ``python-X.Y(pkg1, pkg2)``  → python with packages (version pins stripped)
    - ``python``                   → ``pkgs.python3``
    - ``python@3.11``              → ``pkgs.python311``
    - ``node``                     → ``pkgs.nodejs``
    - ``node@22``                  → ``pkgs.nodejs_22``
    - ``tarball:URL:/path``        → fetchurl derivation
    - ``bare-tool``                → ``pkgs.{tool}``
    """
    stripped = entry.strip()

    # --- python-X.Y(pkg1, pkg2) or node-X(pkg1, pkg2) ---
    m = re.match(r"^(python|node)-(\d+(?:\.\d+)?)\((.+)\)$", stripped)
    if m:
        tool, version, pkgs_raw = m.group(1), m.group(2), m.group(3)
        attr = _versioned_attr(tool, version)
        # Strip version pins like ==1.26.0 from package names
        pkgs = [re.sub(r"[=<>!~]+.*", "", p.strip()) for p in pkgs_raw.split(",")]
        if tool == "python":
            pkg_list = " ".join(f"ps.{p}" for p in pkgs)
            return f"(pkgs.{attr}.withPackages (ps: [ {pkg_list} ]))"
        # node with packages — use nodePackages
        pkg_list = " ".join(f"pkgs.nodePackages.{p}" for p in pkgs)
        return f"pkgs.{attr} {pkg_list}"

    # --- tarball:URL:/path ---
    tm = re.match(r"^tarball:(.+):(/\S+)$", stripped)
    if tm:
        url, path = tm.group(1), tm.group(2)
        return (
            f'(pkgs.runCommand "mshkn-tarball" {{\n'
            f"      src = builtins.fetchurl \"{url}\";\n"
            f"    }} ''\n"
            f"      mkdir -p $out/extract{path}\n"
            f"      tar xf $src -C $out/extract{path} --strip-components=1 || "
            f"cp $src $out/extract{path}/\n"
            f"    '')"
        )

    # --- tool@version ---
    vm = re.match(r"^(\w+)@(.+)$", stripped)
    if vm:
        tool, version = vm.group(1), vm.group(2)
        return f"pkgs.{_versioned_attr(tool, version)}"

    # --- bare tool (with aliases) ---
    alias = _BARE_ALIASES.get(stripped, stripped)
    return f"pkgs.{alias}"


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
