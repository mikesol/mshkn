# Deploying mshkn from scratch

Fresh-server setup. Written against a Hetzner dedicated server (see `docs/infrastructure.md` for the minimum) running Ubuntu 24.04, logged in as root over SSH. Every step was executed verbatim on 2026-09-04 on a Ryzen 7 7700 / 64 GB / 2×1 TB NVMe auction box; where the old procedure was wrong it has been corrected here.

Conventions: `<ip>` is the server's public IPv4; commands run on the server unless marked "dev machine".

## 0. Provision and install the OS

Hetzner delivers dedicated servers booted into the Rescue System. Install Ubuntu 24.04 non-interactively with software RAID1 across the two NVMe drives and a UEFI layout:

```bash
/root/.oldroot/nfs/install/installimage -a -s en -n mshkn -r yes -l 1 \
  -i /root/images/Ubuntu-2404-noble-amd64-base.tar.zst \
  -p "/boot/efi:esp:256M,/boot:ext4:1G,swap:swap:8G,/:ext4:all" \
  -K /root/.ssh/robot_user_keys
reboot
```

`installimage` is not on `PATH` in the rescue system; use the full path. `-K` carries the Robot-registered key into the installed system. The installed system generates new host keys, so re-pin them in `known_hosts` on the dev machine after the reboot.

## 1. System packages

```bash
export DEBIAN_FRONTEND=noninteractive
apt-get update && apt-get install -y \
  debootstrap e2fsprogs thin-provisioning-tools \
  python3.12-venv git rclone curl jq sqlite3 docker.io iptables
systemctl enable --now docker
curl -LsSf https://astral.sh/uv/install.sh | sh     # installs ~/.local/bin/uv
```

Docker builds recipes and the base image; `sqlite3` is handy for inspecting `/opt/mshkn/mshkn.db` directly; uv installs the project.

## 2. Firecracker and kernel

The release asset is a tarball, not a bare binary, and the old "quickstart" kernel URL no longer exists. Use the CI kernel that matches the Firecracker minor version:

```bash
FC=1.14.2
cd /tmp && curl -sL -o fc.tgz https://github.com/firecracker-microvm/firecracker/releases/download/v${FC}/firecracker-v${FC}-x86_64.tgz
tar xzf fc.tgz && install -m 755 release-v${FC}-x86_64/firecracker-v${FC}-x86_64 /usr/local/bin/firecracker
rm -rf fc.tgz release-v${FC}-x86_64
firecracker --version

mkdir -p /opt/firecracker
KERNEL=$(curl -s "http://spec.ccfc.min.s3.amazonaws.com/?prefix=firecracker-ci/v1.14/x86_64/vmlinux-6.1&list-type=2" \
  | grep -oP "(?<=<Key>)firecracker-ci/v1.14/x86_64/vmlinux-6\.1\.[0-9]+(?=</Key>)" | sort -V | tail -1)
curl -sL -o /opt/firecracker/vmlinux.bin "https://s3.amazonaws.com/spec.ccfc.min/$KERNEL"
head -c 4 /opt/firecracker/vmlinux.bin | od -c | head -1   # must print 177 E L F
```

## 3. SSH key for VM access

The orchestrator SSHes into VMs as root with this key; the public half is baked into every rootfs:

```bash
ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N ""
```

## 4. Clone and install mshkn

```bash
cd /opt && git clone https://github.com/mikesol/mshkn.git && cd mshkn
~/.local/bin/uv sync --frozen
.venv/bin/python -c "import mshkn.main; print('import ok')"
```

## 5. Base rootfs

Builds a 1 GB ext4 image with a debootstrapped Ubuntu 24.04, sshd, the MAC-encoded network setup, and the key from step 3. Takes a few minutes.

```bash
cd /opt/firecracker && bash /opt/mshkn/scripts/build-rootfs.sh rootfs.ext4
```

## 6. dm-thin pool, base volume, VM egress

Loop devices and device-mapper tables do not survive a reboot, and Docker sets the `FORWARD` policy to `DROP`, which silently blocks all traffic back into VMs. Both are handled by `scripts/mshkn-pool-up`, run at boot by `systemd/mshkn-pool.service` before the orchestrator starts:

```bash
install -m 755 /opt/mshkn/scripts/mshkn-pool-up /usr/local/sbin/mshkn-pool-up
cp /opt/mshkn/systemd/mshkn-pool.service /etc/systemd/system/
echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-mshkn.conf
systemctl daemon-reload && systemctl enable --now mshkn-pool
dmsetup ls            # mshkn-pool and mshkn-base
```

Write the rootfs into the base volume (thin volume 0) and grow the filesystem to the 8 GiB volume size:

```bash
dd if=/opt/firecracker/rootfs.ext4 of=/dev/mapper/mshkn-base bs=4M status=none
resize2fs /dev/mapper/mshkn-base
e2fsck -fn /dev/mapper/mshkn-base
```

## 7. Base Docker image for recipes

Recipes are Dockerfiles that start `FROM mshkn-base`. Build it once with the VM key in the context:

```bash
mkdir -p /tmp/mshkn-base-build
cp /opt/mshkn/Dockerfile.mshkn-base /tmp/mshkn-base-build/Dockerfile
cp /root/.ssh/id_ed25519.pub /tmp/mshkn-base-build/mshkn_key.pub
docker build -t mshkn-base /tmp/mshkn-base-build
```

## 8. Environment and R2

Create `/opt/mshkn/.env` (mode 600):

```bash
R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=<key>
R2_SECRET_ACCESS_KEY=<secret>
R2_BUCKET=mshkn-checkpoints
MSHKN_IDLE_TIMEOUT=120
MSHKN_CHECKPOINT_RETENTION=5
```

Configure rclone for R2. Do not set an ACL: R2 rejects `x-amz-acl` with a 403, which makes every upload fail. Bucket-scoped tokens cannot list buckets, so verify against the bucket itself:

```bash
set -a; . /opt/mshkn/.env; set +a
rclone config create r2 s3 provider=Cloudflare access_key_id="$R2_ACCESS_KEY_ID" \
  secret_access_key="$R2_SECRET_ACCESS_KEY" endpoint="$R2_ENDPOINT" no_check_bucket=true
echo probe > /tmp/probe.txt
rclone copyto /tmp/probe.txt "r2:$R2_BUCKET/_probe/probe.txt" && rclone purge "r2:$R2_BUCKET/_probe/" && echo "r2 ok"
```

## 9. Orchestrator service

```bash
cp /opt/mshkn/systemd/mshkn.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now mshkn
curl -s localhost:8000/health
```

The first start runs the migrations and creates `/opt/mshkn/mshkn.db`.

## 10. Test account

```bash
cd /opt/mshkn && (.venv/bin/python -m mshkn accounts list | grep -q '^acct-mike	' \
  || .venv/bin/python -m mshkn accounts create --id acct-mike --api-key 'mk-test-key-2026' --vm-limit 20)
```

## 11. Caddy (TLS reverse proxy)

Caddy needs the Cloudflare DNS module for the wildcard certificate, and the orchestrator needs Caddy's admin API up or every create fails while registering its route.

```bash
curl -sL -o /usr/bin/caddy "https://caddyserver.com/api/download?os=linux&arch=amd64&p=github.com/caddy-dns/cloudflare"
chmod +x /usr/bin/caddy && caddy version
mkdir -p /etc/caddy
printf 'CLOUDFLARE_API_TOKEN=%s\n' '<token with Zone:Read and DNS:Edit on mshkn.dev>' > /etc/caddy/env && chmod 600 /etc/caddy/env
```

`/etc/caddy/caddy.json`:

```json
{
  "admin": {"listen": "localhost:2019"},
  "apps": {
    "tls": {
      "certificates": {"automate": ["*.mshkn.dev"]},
      "automation": {"policies": [{"subjects": ["*.mshkn.dev", "mshkn.dev"],
        "issuers": [{"module": "acme", "challenges": {"dns": {"provider": {"name": "cloudflare", "api_token": "{env.CLOUDFLARE_API_TOKEN}"}}}}]}]}
    },
    "http": {"servers": {"main": {"listen": [":443", ":80"],
      "routes": [{"@id": "route-api", "match": [{"host": ["api.mshkn.dev"]}],
        "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "localhost:8000"}]}]}]}}}
  }
}
```

`/etc/systemd/system/caddy.service`:

```ini
[Unit]
Description=Caddy reverse proxy
After=network.target
Wants=network-online.target

[Service]
Type=notify
EnvironmentFile=/etc/caddy/env
ExecStart=/usr/bin/caddy run --config /etc/caddy/caddy.json
ExecReload=/usr/bin/caddy reload --config /etc/caddy/caddy.json
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now caddy
curl -s localhost:2019/config/apps/http/servers/main/routes | head -c 100
```

DNS: `*.mshkn.dev` and `api.mshkn.dev` A records point at `<ip>`.

## 12. Litestream (SQLite replication to R2)

```bash
curl -sL -o /tmp/litestream.deb https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-linux-amd64.deb
dpkg -i /tmp/litestream.deb
```

`/etc/litestream.yml` (mode 600), with the values from `.env`:

```yaml
dbs:
  - path: /opt/mshkn/mshkn.db
    replicas:
      - type: s3
        bucket: mshkn-checkpoints
        path: litestream/mshkn.db
        endpoint: <R2_ENDPOINT>
        access-key-id: <R2_ACCESS_KEY_ID>
        secret-access-key: <R2_SECRET_ACCESS_KEY>
        force-path-style: true
```

```bash
cp /opt/mshkn/systemd/litestream.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now litestream
journalctl -u litestream -n 3 --no-pager    # "wal segment written"
```

`litestream.service` is `PartOf=mshkn.service`, so restarting mshkn restarts it.

## 13. Verify

```bash
systemctl status mshkn-pool mshkn caddy litestream --no-pager | grep -E "●|Active"
H="Authorization: Bearer mk-test-key-2026"
ID=$(curl -s -X POST localhost:8000/computers -H "$H" -H "Content-Type: application/json" -d '{}' | jq -r .computer_id)
curl -s -N -X POST "localhost:8000/computers/$ID/exec" -H "$H" -H "Content-Type: application/json" \
  -d '{"command":"echo hello; curl -s -m 5 -o /dev/null -w %{http_code} https://example.com"}'
curl -s -X DELETE "localhost:8000/computers/$ID" -H "$H"
```

Expect `hello` and `200` (the second line proves VM egress). Then from the dev machine, with an ssh config alias for the server:

```
Host mshkn
  HostName <ip>
  User root
  IdentityFile ~/.ssh/<deploy key>
  IdentitiesOnly yes
```

```bash
MSHKN_SERVER=mshkn MSHKN_API_URL=http://<ip>:8000 scripts/e2e.sh
```

## Teardown

Kill all VMs and wipe state (the pool is recreated by `mshkn-pool.service` on the next start):

```bash
systemctl stop mshkn litestream
pkill -x firecracker || true
for tap in $(ip -o link show type tun | awk -F': ' '{print $2}'); do ip link del "$tap"; done
for vol in $(dmsetup ls --target thin | awk '{print $1}'); do dmsetup remove "$vol" || true; done
dmsetup remove mshkn-pool || true
losetup -D
rm -f /opt/mshkn/thin-pool-{data,meta} /opt/mshkn/mshkn.db
systemctl restart mshkn-pool   # recreates the empty pool and base volume
```

Then redo step 6's `dd`/`resize2fs` and step 10.
