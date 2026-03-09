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
debootstrap --variant=minbase --include=openssh-server,bash,coreutils,ca-certificates,iproute2,iputils-ping,curl,sudo,e2fsprogs,util-linux,udev,systemd,dbus \
    noble "$ROOTFS_DIR" http://archive.ubuntu.com/ubuntu

echo "==> Configure SSH"
mkdir -p "$ROOTFS_DIR/root/.ssh"
sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' "$ROOTFS_DIR/etc/ssh/sshd_config"
sed -i 's/PermitRootLogin prohibit-password/PermitRootLogin yes/' "$ROOTFS_DIR/etc/ssh/sshd_config"
chroot "$ROOTFS_DIR" ssh-keygen -A

echo "==> Configure networking (Firecracker MAC-based IP)"
# fcnet-setup.sh decodes the IP from the MAC address set by the host
cat > "$ROOTFS_DIR/usr/local/bin/fcnet-setup.sh" <<'FCNET'
#!/bin/bash
for dev in $(ls /sys/class/net | grep -v lo); do
    mac_ip=$(ip link show dev "$dev" | grep link/ether | grep -oP "(?<=06:00:)[0-9a-f:]{11}")
    if [ -n "$mac_ip" ]; then
        ip=$(printf "%d.%d.%d.%d" $(echo "0x${mac_ip}" | sed "s/:/ 0x/g"))
        ip addr add "$ip/30" dev "$dev"
        ip link set "$dev" up
        gw=$(echo "$ip" | awk -F. '{printf "%d.%d.%d.%d", $1, $2, $3, $4-1}')
        ip route add default via "$gw"
    fi
done
FCNET
chmod +x "$ROOTFS_DIR/usr/local/bin/fcnet-setup.sh"

# systemd service for fcnet
mkdir -p "$ROOTFS_DIR/etc/systemd/system/sysinit.target.wants"
cat > "$ROOTFS_DIR/etc/systemd/system/fcnet.service" <<'FCNET_SVC'
[Unit]
Description=Firecracker network setup
DefaultDependencies=no
After=systemd-udevd.service
Before=network.target
Wants=ssh.service

[Service]
Type=oneshot
ExecStartPre=/usr/bin/udevadm settle
ExecStart=/usr/local/bin/fcnet-setup.sh
RemainAfterExit=true
FCNET_SVC
ln -sf /etc/systemd/system/fcnet.service "$ROOTFS_DIR/etc/systemd/system/sysinit.target.wants/fcnet.service"

# Enable SSH on boot
chroot "$ROOTFS_DIR" systemctl enable ssh 2>/dev/null || \
    ln -sf /lib/systemd/system/ssh.service "$ROOTFS_DIR/etc/systemd/system/multi-user.target.wants/ssh.service"

echo "==> Install SSH authorized key"
# Copy the host's public key so the host can SSH into VMs
if [ -f /root/.ssh/id_ed25519.pub ]; then
    cat /root/.ssh/id_ed25519.pub >> "$ROOTFS_DIR/root/.ssh/authorized_keys"
    chmod 600 "$ROOTFS_DIR/root/.ssh/authorized_keys"
fi

echo "==> Remove apt/dpkg to enforce purity"
rm -f "$ROOTFS_DIR/usr/bin/apt" "$ROOTFS_DIR/usr/bin/apt-get" "$ROOTFS_DIR/usr/bin/apt-cache"
rm -f "$ROOTFS_DIR/usr/bin/dpkg" "$ROOTFS_DIR/usr/bin/dpkg-deb"
rm -rf "$ROOTFS_DIR/var/lib/apt/lists"/* "$ROOTFS_DIR/var/cache/apt"/*

echo "==> Ensure /sbin/init symlink"
ln -sf ../lib/systemd/systemd "$ROOTFS_DIR/sbin/init"

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
