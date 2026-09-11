# Infrastructure requirements

mshkn's unit and flow tests run anywhere. The E2E suite (`tests/e2e/`, 163 tests) is the source of truth for the product and needs a real host that can run Firecracker microVMs, dm-thin block devices, Docker recipe builds, and a public HTTPS proxy. This page states the minimum that host must provide so it can be rented before work that depends on it starts.

## Minimum host

| Requirement | Minimum | Why |
|---|---|---|
| Virtualization | Bare-metal x86_64 with `/dev/kvm`, or a VM whose provider exposes nested virtualization | Firecracker needs KVM. Hetzner Cloud VPSes do not expose it; Hetzner dedicated servers (including the server auction) do. |
| OS | Ubuntu 24.04 LTS, root SSH | DEPLOY.md is written against it; systemd, dm-thin, and Docker packages are current. |
| CPU | 4 cores (6 or more recommended) | Concurrency tests boot up to 20 VMs at once; recipe builds run `docker build` pinned to 2 cores. |
| RAM | 16 GB (32 GB or more recommended) | 20 VMs × 256 MiB plus a 4 GB Docker build plus the L3 template memory files. T5.6 additionally boots a single 8 GB VM. |
| Disk | 250 GB NVMe | 100 GB sparse thin-pool file, Docker image cache, local checkpoint snapshots (vmstate + 256 MiB memory each). SATA works but latency targets are calibrated on NVMe. |
| Network | Public IPv4, outbound internet | VMs reach the internet through host NAT; tests fetch packages inside VMs; Caddy answers on 80/443. |
| Kernel | `dm_thin_pool` module and `thin-provisioning-tools` | dm-thin copy-on-write snapshots are how fork is O(1). |
| Ubuntu apt mirror | A mirror the host reaches at MB/s, carrying `<release>` **and** `<release>-security` | Every recipe builds `FROM mshkn-base` and every bare computer boots it, so the image's apt sources are where the whole product resolves packages. `archive.ubuntu.com` is not reliably reachable from every provider — from the current host it returned 0 bytes in 30 s while `mirror.hetzner.com` served 26 MB/s — and apt answers an unreachable mirror with a stall, not an error, so a bad one costs ten minutes per build (#137). Set it as `MSHKN_APT_MIRROR` (DEPLOY.md step 7) and rebuild the base volume. |

Docker on Ubuntu 24.04 (`docker.io`) ships without the buildx plugin, so `docker build` runs the deprecated legacy builder; mshkn keeps each recipe's image so that builder's layer cache serves rebuilds. If a Docker upgrade removes the legacy builder, install `docker-buildx` and re-check the recipe build log format, which BuildKit changes.

The previous host was a Hetzner AX41-NVMe (Ryzen 5 3600, 64 GB, 2×512 GB NVMe). Anything in that class is comfortable.

## A model gateway, for measuring across models (#123 round, spec §11)

Spec §11 says the liturgy is spoken "across models where useful", and the post-cut round in
`docs/embryo/README.md` cost about $10.60 for six runs of one model. Both want a second provider.

`embryo/membrane/model.py` speaks one wire format end to end: `compose_request` builds the Anthropic Messages request body
that `/v1/messages` accepts (`system`, `messages`, `tools`, `output_config.effort`) and `parse_message`
reads Anthropic content blocks including `tool_use`. `embryo/hatch.sh:91` scopes the brain's key to
exactly one relay target, `"$ANTHROPIC_BASE_URL/"`. So the cheapest way to reach another model is a
gateway that speaks the Anthropic Messages API and fans out behind it — not a second code path in
the membrane, which would put provider handling inside the organism.

| Requirement | Minimum | Why |
|---|---|---|
| A LiteLLM proxy reachable over HTTPS from the mshkn host | One small always-on instance, or a hosted equivalent | The brain relays through the host; the host must reach it. |
| An Anthropic-compatible `/v1/messages` endpoint on it | Must accept `system`, `messages`, `tools`, and return `tool_use` content blocks | `model.py` composes and parses nothing else; anything less means changing the membrane. |
| Provider credentials held by the proxy | At least one non-Anthropic provider | The point is a second model; the brain's scoped key never sees these. |
| A stable base URL | Set as `ANTHROPIC_BASE_URL` at hatch time | `hatch.sh:91` bakes it into the key's `relay.targets`, so it must not move between hatch and run. |

Two things to record rather than discover: `output_config.effort` is Anthropic-specific and has no
equivalent elsewhere, so cross-provider runs cannot be compared on the effort axis — which is
awkward, since the 2026-09-10 round's headline was that medium effort beat the default. And
tool-use fidelity varies by backend, so a low score on a cheaper model may be measuring the
gateway's translation rather than the organism.

## Accounts and secrets the host setup needs

| Item | Purpose | Status |
|---|---|---|
| Cloudflare DNS: `mshkn.dev` and `*.mshkn.dev` A records → host IP | Caddy routes `{port}-{computer_id}.mshkn.dev` to VMs; `api.mshkn.dev` to the orchestrator | Must be repointed to the new host |
| Cloudflare API token with DNS:Edit on `mshkn.dev` | Caddy's DNS-01 challenge for the wildcard certificate | Previously named `mshkn-caddy-dns`; needs the value, or a new token |
| R2 bucket `mshkn-checkpoints` with access key and secret | Checkpoint upload and Litestream replication | Present in the local `.env`; nothing to do unless rotated |
| Operator SSH public key in `/root/.ssh/authorized_keys` | Deploy and E2E scripts | `~/.ssh/id_ed25519.pub` on the dev machine |
| `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` | Real-model runs of the embryo (`embryo/hatch.sh` with `MEMBRANE_MODEL=anthropic`; `uv run measure`), billed per token. The E2E suite does not need them: Phase 14 runs the scripted model with a local embedder. | In the operator's local `.env` (git-ignored) beside the R2 keys, or in the environment; stored nowhere in this repository. The OpenAI account must carry a credit balance: without one the brain's first memory recall fails with `insufficient_quota` and every turn exits 1 (found 2026-09-09, the first attempt at #101). One liturgy embeds a few thousand tokens with `text-embedding-3-small`; a few dollars last for many runs. |

## What the operator provides, in order

1. The rented host's IP address, with root SSH accepting the operator's key.
2. DNS repointed (or a DNS-edit token so it can be scripted).
3. The Caddy DNS-01 token.

Setup then follows `DEPLOY.md` verbatim; `scripts/e2e.sh` with `MSHKN_SERVER=root@<ip>` runs the suite.

## Not sufficient

- The developer laptop: has KVM but would run the orchestrator as root with tap devices and iptables rules on a workstation, and cannot serve the wildcard domain.
- General-purpose cloud VMs without nested virtualization (checked 2026-09-04: two available 4-vCPU, 7 GB hosts have no `/dev/kvm`).
