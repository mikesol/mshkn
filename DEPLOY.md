# Deploying mshkn from scratch

Fresh server setup guide. Assumes a bare Hetzner AX41-NVMe (or equivalent: AMD64, 64GB+ RAM, NVMe storage) running Ubuntu 24.04.

## 1. System packages

```bash
apt update && apt install -y \
  debootstrap e2fsprogs thin-provisioning-tools \
  python3.12 python3.12-venv python3-pip \
  git rclone curl jq
```

## 2. Firecracker

```bash
FC_VERSION=1.14.2
curl -L -o /usr/local/bin/firecracker \
  https://github.com/firecracker-microvm/firecracker/releases/download/v${FC_VERSION}/firecracker-v${FC_VERSION}-x86_64
chmod +x /usr/local/bin/firecracker
```

## 3. Kernel

Download a 6.1.x kernel built for Firecracker:

```bash
mkdir -p /opt/firecracker
curl -L -o /opt/firecracker/vmlinux.bin \
  https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/x86_64/kernels/vmlinux-6.1.102
```

## 4. SSH key for VM access

The host SSH key gets baked into the rootfs so the orchestrator can SSH into VMs:

```bash
ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N ""
```

## 5. Rootfs

Build the base VM rootfs using the included script:

```bash
cd /opt/firecracker
cp /opt/mshkn/scripts/build-rootfs.sh . 2>/dev/null || cp /opt/firecracker/build-rootfs.sh .
chmod +x build-rootfs.sh
./build-rootfs.sh rootfs.ext4
```

This creates a 1GB ext4 image with: debootstrap Ubuntu 24.04 (minimal), SSH server, Firecracker network setup (MAC-encoded IP), minimal PID 1 init (`/sbin/mshkn-init`), purity shims (apt/pip/npm blocked with structured error messages), and the host's SSH pubkey baked in.

## 6. dm-thin pool

The thin-provisioning pool enables O(1) copy-on-write disk snapshots for VM forking:

```bash
mkdir -p /opt/mshkn
truncate -s 100G /opt/mshkn/thin-pool-data
truncate -s 256M /opt/mshkn/thin-pool-meta

DATA_LOOP=$(losetup --find --show /opt/mshkn/thin-pool-data)
META_LOOP=$(losetup --find --show /opt/mshkn/thin-pool-meta)
DATA_SECTORS=$(blockdev --getsz "$DATA_LOOP")
dmsetup create mshkn-pool --table "0 $DATA_SECTORS thin-pool $META_LOOP $DATA_LOOP 512 0"
```

Create the base volume and write the rootfs:

```bash
# Create thin volume 0 (base) with 8GB (16777216 sectors)
dmsetup message mshkn-pool 0 "create_thin 0"
dmsetup create mshkn-base --table "0 16777216 thin /dev/mapper/mshkn-pool 0"

# Write rootfs and resize to fill 8GB
dd if=/opt/firecracker/rootfs.ext4 of=/dev/mapper/mshkn-base bs=4M
resize2fs /dev/mapper/mshkn-base
```

## 7. Clone and install mshkn

```bash
cd /opt
git clone https://github.com/mikesol/mshkn.git
cd mshkn
python3.12 -m venv .venv
.venv/bin/pip install -e .
```

## 8. Environment variables

Create `/opt/mshkn/.env`:

```bash
# Cloudflare R2 (for checkpoint storage)
R2_TOKEN=<your-r2-token>
R2_ACCESS_KEY_ID=<your-r2-access-key>
R2_SECRET_ACCESS_KEY=<your-r2-secret-key>
R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
R2_BUCKET=mshkn-checkpoints
MSHKN_IDLE_TIMEOUT=120
MSHKN_CHECKPOINT_RETENTION=5
```

Configure rclone for R2:

```bash
rclone config create r2 s3 \
  provider=Cloudflare \
  access_key_id=<R2_ACCESS_KEY_ID> \
  secret_access_key=<R2_SECRET_ACCESS_KEY> \
  endpoint=<R2_ENDPOINT> \
  acl=private
```

## 9. Systemd service

Create `/etc/systemd/system/mshkn.service`:

```ini
[Unit]
Description=mshkn orchestrator
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/mshkn
Environment=PATH=/root/.nix-profile/bin:/nix/var/nix/profiles/default/bin:/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/opt/mshkn/.venv/bin/uvicorn mshkn.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
EnvironmentFile=/opt/mshkn/.env

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now mshkn
```

## 10. Caddy (TLS reverse proxy)

Install Caddy with the Cloudflare DNS module (needed for wildcard certs):

```bash
curl -L -o /usr/bin/caddy \
  "https://caddyserver.com/api/download?os=linux&arch=amd64&p=github.com/caddy-dns/cloudflare"
chmod +x /usr/bin/caddy
```

Create `/etc/caddy/caddy.json`:

```json
{
  "admin": {"listen": "localhost:2019"},
  "apps": {
    "tls": {
      "certificates": {"automate": ["*.mshkn.dev"]},
      "automation": {
        "policies": [{
          "subjects": ["*.mshkn.dev", "mshkn.dev"],
          "issuers": [{
            "module": "acme",
            "challenges": {
              "dns": {
                "provider": {
                  "name": "cloudflare",
                  "api_token": "{env.CLOUDFLARE_API_TOKEN}"
                }
              }
            }
          }]
        }]
      }
    },
    "http": {
      "servers": {
        "main": {
          "listen": [":443", ":80"],
          "routes": [{
            "@id": "route-api",
            "match": [{"host": ["api.mshkn.dev"]}],
            "handle": [{
              "handler": "reverse_proxy",
              "upstreams": [{"dial": "localhost:8000"}]
            }]
          }]
        }
      }
    }
  }
}
```

Create `/etc/systemd/system/caddy.service`:

```ini
[Unit]
Description=Caddy reverse proxy
After=network.target
Wants=network-online.target

[Service]
Type=notify
Environment=CLOUDFLARE_API_TOKEN=<your-cloudflare-api-token>
ExecStart=/usr/bin/caddy run --config /etc/caddy/caddy.json
ExecReload=/usr/bin/caddy reload --config /etc/caddy/caddy.json
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now caddy
```

Point DNS: `*.mshkn.dev` and `mshkn.dev` A records to the server IP.

## 11. Litestream (SQLite replication)

```bash
curl -L -o /tmp/litestream.deb \
  https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-linux-amd64.deb
dpkg -i /tmp/litestream.deb
```

Create `/etc/litestream.yml`:

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

Create `/etc/systemd/system/litestream.service`:

```ini
[Unit]
Description=Litestream SQLite replication
After=network.target mshkn.service
Requires=mshkn.service

[Service]
Type=simple
ExecStart=/usr/bin/litestream replicate -config /etc/litestream.yml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now litestream
```

## 12. Create test account

```bash
curl -s http://localhost:8000/accounts -X POST \
  -H "Content-Type: application/json" \
  -d '{"account_id": "acct-mike", "api_key": "mk-test-key-2026", "vm_limit": 20}'
```

## 13. Verify

```bash
# Service health
systemctl status mshkn caddy litestream

# API responds
curl -s http://localhost:8000/health

# From dev machine: E2E tests
MSHKN_API_URL=http://<server-ip>:8000 .venv/bin/pytest tests/e2e/ -v --tb=short
```

## Cleanup / teardown

Kill all VMs and wipe state:

```bash
systemctl stop mshkn litestream
pkill -f firecracker || true
for tap in $(ip -o link show type tun | awk -F: '{print $2}' | tr -d ' '); do ip link del "$tap"; done
for vol in $(dmsetup ls --target thin | awk '{print $1}'); do dmsetup remove "$vol" || true; done
dmsetup remove mshkn-pool || true
losetup -D
rm -f /opt/mshkn/thin-pool-{data,meta} /opt/mshkn/mshkn.db
```

To rebuild, start again from step 6.
