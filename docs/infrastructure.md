# Infrastructure requirements

mshkn's unit and flow tests run anywhere. The E2E suite (`tests/e2e/`, 157 tests) is the source of truth for the product and needs a real host that can run Firecracker microVMs, dm-thin block devices, Docker recipe builds, and a public HTTPS proxy. This page states the minimum that host must provide so it can be rented before work that depends on it starts.

## Minimum host

| Requirement | Minimum | Why |
|---|---|---|
| Virtualization | Bare-metal x86_64 with `/dev/kvm`, or a VM whose provider exposes nested virtualization | Firecracker needs KVM. Hetzner Cloud VPSes do not expose it; Hetzner dedicated servers (including the server auction) do. |
| OS | Ubuntu 24.04 LTS, root SSH | DEPLOY.md is written against it; systemd, dm-thin, and Docker packages are current. |
| CPU | 4 cores (6 or more recommended) | Concurrency tests boot up to 20 VMs at once; recipe builds run `docker build` pinned to 2 cores. |
| RAM | 16 GB (32 GB or more recommended) | 20 VMs × 256 MiB plus a 4 GB Docker build plus the L3 template memory files. |
| Disk | 250 GB NVMe | 100 GB sparse thin-pool file, Docker image cache, local checkpoint snapshots (vmstate + 256 MiB memory each). SATA works but latency targets are calibrated on NVMe. |
| Network | Public IPv4, outbound internet | VMs reach the internet through host NAT; tests fetch packages inside VMs; Caddy answers on 80/443. |
| Kernel | `dm_thin_pool` module and `thin-provisioning-tools` | dm-thin copy-on-write snapshots are how fork is O(1). |

The previous host was a Hetzner AX41-NVMe (Ryzen 5 3600, 64 GB, 2×512 GB NVMe). Anything in that class is comfortable.

## Accounts and secrets the host setup needs

| Item | Purpose | Status |
|---|---|---|
| Cloudflare DNS: `mshkn.dev` and `*.mshkn.dev` A records → host IP | Caddy routes `{port}-{computer_id}.mshkn.dev` to VMs; `api.mshkn.dev` to the orchestrator | Must be repointed to the new host |
| Cloudflare API token with DNS:Edit on `mshkn.dev` | Caddy's DNS-01 challenge for the wildcard certificate | Previously named `mshkn-caddy-dns`; needs the value, or a new token |
| R2 bucket `mshkn-checkpoints` with access key and secret | Checkpoint upload and Litestream replication | Present in the local `.env`; nothing to do unless rotated |
| Operator SSH public key in `/root/.ssh/authorized_keys` | Deploy and E2E scripts | `~/.ssh/id_ed25519.pub` on the dev machine |

## What the operator provides, in order

1. The rented host's IP address, with root SSH accepting the operator's key.
2. DNS repointed (or a DNS-edit token so it can be scripted).
3. The Caddy DNS-01 token.

Setup then follows `DEPLOY.md` verbatim; `scripts/e2e.sh` with `MSHKN_SERVER=root@<ip>` runs the suite.

## Not sufficient

- The developer laptop: has KVM but would run the orchestrator as root with tap devices and iptables rules on a workstation, and cannot serve the wildcard domain.
- General-purpose cloud VMs without nested virtualization (checked 2026-09-04: two available 4-vCPU, 7 GB hosts have no `/dev/kvm`).
