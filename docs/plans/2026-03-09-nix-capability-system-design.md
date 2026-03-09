# Nix Capability System Design

Implements issue #8: the `uses:` parameter that gives VMs capabilities (Python, Node, ffmpeg, etc.) via Nix.

## Architecture

Two-level cache with three components.

### Level 1: Nix Store (host `/nix/store`)

Individual Nix store paths, content-addressed. Persistent across all builds. Cache miss downloads from the nixpkgs binary cache. Never manually evicted (Nix GC handles this).

### Level 2: Capability Base Volumes (dm-thin)

One per unique `manifest_hash`. Contains base rootfs + Nix closure copied into `/nix/store` + symlinks in `/usr/local/bin` + purity shims. VMs snapshot from these instead of from volume 0 (bare base). LRU eviction when disk pressure rises.

### Components

1. **CapabilityResolver** (exists, needs extension) — `manifest → Nix expression`
2. **CapabilityBuilder** (rewrite) — `Nix expression → capability base volume (dm-thin)`
3. **CapabilityCache** (exists, needs integration) — `manifest_hash → volume_id lookup`

### Flow

```
POST /computers {uses: ["python-3.12(numpy)", "ffmpeg"]}
  1. Hash manifest
  2. Cache lookup: capability_cache table → volume_id?
  3. HIT:  snapshot from that volume_id → boot
  4. MISS: nix-build → mount base volume snapshot → copy closure in →
          register as capability base volume → snapshot → boot
```

## Capability Base Volume Construction (cache miss)

1. **Resolve**: `manifest_to_nix(uses)` → Nix expression
2. **Build**: `nix-build --no-out-link /tmp/capability.nix` → `/nix/store/abc123-mshkn-capability`
3. **Compose**: Create a new dm-thin volume and populate it:
   - `create_thin` new volume in pool
   - `dd` base rootfs onto it
   - Mount on a temp mountpoint
   - Copy full Nix closure (`nix-store -qR` for transitive deps) into mountpoint's `/nix/store/`
   - Create symlinks in `/usr/local/bin` → `/nix/store/.../bin/*`
   - Install purity shims (pip/npm/apt wrappers)
   - Set immutable bit on `/nix/store`: `chattr +i -R /nix/store`
   - Unmount
4. **Register**: Insert into `capability_cache` table (`manifest_hash → volume_id`)
5. **Snapshot**: `create_snapshot` from this capability volume → VM's volume → boot

The capability volume is never modified after construction. It is a read-only template.

## Purity Enforcement

Package managers are blocked inside the VM with structured errors.

### Mechanism

1. **Base image has no apt/dpkg.** Built from scratch with debootstrap — only openssh-server, coreutils, bash, ca-certificates.
2. **Wrapper scripts** replace pip/npm/apt on PATH:
   - `/usr/local/bin/pip` → shim that prints structured JSON error and exits 1
   - `/usr/local/bin/npm` → same
   - `/usr/local/bin/apt-get`, `apt`, `dpkg` → same
   - Shims are earlier on PATH than `/usr/bin`, intercepting all calls.
3. **`/nix/store` is immutable.** After capability injection, `chattr +i -R /nix/store` prevents writes even by root. Satisfies T3.4.

### Structured Error Format

```json
{
  "error": "Package installation not permitted in mutable layer.",
  "suggested_action": {
    "tool": "checkpoint_fork",
    "args": {
      "uses": ["python-3.12(numpy, pandas, requests)", "ffmpeg"]
    }
  }
}
```

The shim parses the pip/npm command to extract the requested package name and includes it in the suggested manifest (current manifest + new package).

## Resolver Extensions

### Python with version pinning

`python-3.12(numpy==1.26.0)` → generates `fetchPypi`-based Nix expression:

```nix
buildPythonPackage {
  pname = "numpy";
  version = "1.26.0";
  src = fetchPypi { pname = "numpy"; version = "1.26.0"; sha256 = "..."; };
}
```

Unpinned packages (`numpy` without `==`) use whatever nixpkgs channel provides.

### Node

`node-22(express, react)` → `pkgs.nodejs-22_x` + `pkgs.nodePackages.{pkg}` in the buildEnv paths.

### Tarball escape hatch

`tarball("https://example.com/tool.tar.gz")` → Nix `fetchurl` derivation:

```nix
pkgs.runCommand "mshkn-tarball-{hash}" {
  src = pkgs.fetchurl { url = "..."; sha256 = "..."; };
} ''
  mkdir -p $out/opt $out/usr/local/bin
  tar xf $src -C $out/opt
  for f in $out/opt/*/bin/*; do
    ln -s $f $out/usr/local/bin/$(basename $f)
  done
''
```

The sha256 is computed by downloading the URL once before passing to Nix.

### Bare tools

`ffmpeg` → `pkgs.ffmpeg` (unchanged from current resolver).

## Manifest Compatibility on Fork (T3.9)

When forking from a checkpoint with a different manifest:

- **Additive** (new manifest is superset of parent's `uses`) → allowed
- **Breaking** (removal or version change) → requires `skip_manifest_check: true` in the fork API call; downgrades error to warning in response
- **Identical** → allowed

Fork with different capabilities = checkpoint disk state + fresh capability base volume. The agent's files survive, but the toolchain changes.

## Base Image

New rootfs built from a reproducible script in the repo:

1. `debootstrap` minimal Ubuntu 24.04
2. Install only: openssh-server, coreutils, bash, ca-certificates
3. No apt/dpkg/pip/npm
4. Pre-create `/nix` directory structure
5. Set up PATH to include Nix profile paths in `/etc/profile`
6. Configure SSH (root login, key auth)
7. Output `rootfs.ext4`, replace existing base image

## Volume Size

Increase `thin_volume_sectors` from 4194304 (2GB) to 16777216 (8GB) to accommodate large Nix closures + user data. Actual disk usage remains CoW — only written blocks consume pool space.

## LRU Eviction

Implemented towards the end of the build. When disk pressure rises:

1. Query `capability_cache` ordered by `last_used_at` ascending
2. Remove the least-recently-used capability base volume (dm-thin delete + cache row delete)
3. Repeat until free space is above threshold

## Test Mapping

| Test | What | Mechanism |
|------|------|-----------|
| T3.1 | pip install blocked | Purity shim returns structured JSON |
| T3.2 | npm install blocked | Purity shim returns structured JSON |
| T3.3 | apt-get blocked | apt not in base image + shim for helpful error |
| T3.4 | /nix/store read-only | `chattr +i` immutable bit |
| T3.5 | Capability caching | Level 2 cache hit → fast snapshot |
| T3.6 | Composition | Single buildEnv with python + node + ffmpeg |
| T3.7 | Version pinning | fetchPypi with exact version |
| T3.8 | Tarball escape hatch | fetchurl Nix derivation |
| T3.9 | Manifest compat | Fork API compares manifests, warns/blocks on breaking |
| T10.1-T10.2 | Agent workflows | Pass naturally once T3.1-T3.9 work |
