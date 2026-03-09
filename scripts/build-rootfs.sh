#!/bin/bash
# Build a minimal rootfs for mshkn VMs.
# Requires: debootstrap, root privileges
# Output: rootfs.ext4 in the current directory
set -euo pipefail

OUTPUT="${1:-rootfs.ext4}"
SIZE_MB=1024
ROOTFS_DIR=$(mktemp -d /tmp/mshkn-rootfs.XXXXXX)

cleanup() {
    umount "$ROOTFS_DIR/proc" 2>/dev/null || true
    umount "$ROOTFS_DIR/sys" 2>/dev/null || true
    umount "$ROOTFS_DIR/dev" 2>/dev/null || true
    rm -rf "$ROOTFS_DIR"
}
trap cleanup EXIT

echo "==> debootstrap minimal Ubuntu 24.04"
debootstrap --variant=minbase --include=openssh-server,bash,coreutils,ca-certificates,iproute2,iputils-ping,curl,sudo,e2fsprogs,util-linux \
    noble "$ROOTFS_DIR" http://archive.ubuntu.com/ubuntu

echo "==> Configure SSH"
mkdir -p "$ROOTFS_DIR/root/.ssh"
sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' "$ROOTFS_DIR/etc/ssh/sshd_config"
sed -i 's/PermitRootLogin prohibit-password/PermitRootLogin yes/' "$ROOTFS_DIR/etc/ssh/sshd_config"
chroot "$ROOTFS_DIR" ssh-keygen -A

echo "==> Configure networking"
cat > "$ROOTFS_DIR/etc/network/interfaces" <<'IFACES'
auto lo
iface lo inet loopback

auto eth0
iface eth0 inet dhcp
IFACES

# Enable SSH on boot
chroot "$ROOTFS_DIR" systemctl enable ssh 2>/dev/null || \
    ln -sf /lib/systemd/system/ssh.service "$ROOTFS_DIR/etc/systemd/system/multi-user.target.wants/ssh.service"

echo "==> Remove apt/dpkg to enforce purity"
rm -f "$ROOTFS_DIR/usr/bin/apt" "$ROOTFS_DIR/usr/bin/apt-get" "$ROOTFS_DIR/usr/bin/apt-cache"
rm -f "$ROOTFS_DIR/usr/bin/dpkg" "$ROOTFS_DIR/usr/bin/dpkg-deb"
rm -rf "$ROOTFS_DIR/var/lib/apt/lists"/* "$ROOTFS_DIR/var/cache/apt"/*

echo "==> Pre-create /nix structure"
mkdir -p "$ROOTFS_DIR/nix/store"
mkdir -p "$ROOTFS_DIR/nix/var/nix"

echo "==> Set up PATH for Nix"
cat >> "$ROOTFS_DIR/etc/profile" <<'PROFILE'

# mshkn: add /usr/local/bin to PATH
export PATH="/usr/local/bin:$PATH"
PROFILE

cat >> "$ROOTFS_DIR/root/.bashrc" <<'BASHRC'
export PATH="/usr/local/bin:$PATH"
BASHRC

echo "==> Install purity shims"

# apt-get shim
cat > "$ROOTFS_DIR/usr/local/bin/apt-get" <<'SHIM'
#!/bin/bash
cat >&2 <<'JSON'
{
  "error": "Package installation not permitted. Use the 'uses' capability manifest instead.",
  "suggested_action": {
    "tool": "checkpoint_fork",
    "args": {"uses": ["<add the package you need to the uses manifest>"]}
  }
}
JSON
exit 1
SHIM
chmod +x "$ROOTFS_DIR/usr/local/bin/apt-get"
cp "$ROOTFS_DIR/usr/local/bin/apt-get" "$ROOTFS_DIR/usr/local/bin/apt"
chmod +x "$ROOTFS_DIR/usr/local/bin/apt"
cp "$ROOTFS_DIR/usr/local/bin/apt-get" "$ROOTFS_DIR/usr/local/bin/dpkg"
chmod +x "$ROOTFS_DIR/usr/local/bin/dpkg"

# pip shim (even on bare VMs without Python)
cat > "$ROOTFS_DIR/usr/local/bin/pip" <<'SHIM'
#!/bin/bash
PKG="${@: -1}"
cat >&2 <<JSON
{
  "error": "Package installation not permitted. Use the 'uses' capability manifest instead.",
  "suggested_action": {
    "tool": "checkpoint_fork",
    "args": {"uses": ["python($PKG)"]}
  }
}
JSON
exit 1
SHIM
chmod +x "$ROOTFS_DIR/usr/local/bin/pip"
cp "$ROOTFS_DIR/usr/local/bin/pip" "$ROOTFS_DIR/usr/local/bin/pip3"
chmod +x "$ROOTFS_DIR/usr/local/bin/pip3"

# npm shim (even on bare VMs without Node)
cat > "$ROOTFS_DIR/usr/local/bin/npm" <<'SHIM'
#!/bin/bash
PKG="${@: -1}"
cat >&2 <<JSON
{
  "error": "Package installation not permitted. Use the 'uses' capability manifest instead.",
  "suggested_action": {
    "tool": "checkpoint_fork",
    "args": {"uses": ["node($PKG)"]}
  }
}
JSON
exit 1
SHIM
chmod +x "$ROOTFS_DIR/usr/local/bin/npm"

echo "==> Create ext4 image"
dd if=/dev/zero of="$OUTPUT" bs=1M count=$SIZE_MB
mkfs.ext4 -d "$ROOTFS_DIR" "$OUTPUT"

echo "==> Done: $OUTPUT (${SIZE_MB}MB)"
