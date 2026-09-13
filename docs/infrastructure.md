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

## A model gateway, for measuring across models (#127, spec §11)

Spec §11 says a capability is spoken "across models where useful", and the round in
`docs/embryo/hatch/README.md` cost about $10.60 for six runs of one model. Both want a second
provider.

`embryo/membrane/model.py` speaks one wire format end to end, and it stays that way: mshkn reaches
a second model through a hosted gateway that speaks the Anthropic Messages API, not through a
second code path in the membrane. Vercel AI Gateway is what this is written against. It accepts
`x-api-key` and `anthropic-version`, which is what `request_headers` already sends, and it
namespaces model ids by provider (`anthropic/claude-opus-5`).

There is no host to rent. What the operator provides:

| Item | Purpose | Status |
|---|---|---|
| `AI_GATEWAY_API_KEY` | The gateway key, sent as `x-api-key` when `ANTHROPIC_BASE_URL` is not the Anthropic API. `hatch.sh` writes it into `/brain/.env` under the name `ANTHROPIC_API_KEY`: the brain is handed one model key and never learns which kind it is. | In the operator's local `.env` beside the other keys; stored nowhere in this repository. |
| A payment method on the Vercel team | Without one the gateway answers every request `403 customer_verification_required`, and its free credits stay locked. | Checked 2026-09-13. |
| A spend budget on that key | A brain that loops bills per turn, and the budget is the only stop that does not depend on the organism behaving. | Set in the Vercel dashboard, not in this repository. |
| The operator's Anthropic key in the gateway's team BYOK settings | Optional. BYOK carries no markup, so a run through the gateway costs what the same run cost directly and stays comparable to the evidence already in `docs/embryo/`. | Optional. |
| `MEMBRANE_BODY_EXTRA` / `--body-extra` | The reproducibility pin spec §8 calls essential: the gateway may route a model id to more than one upstream, and this opaque JSON object (e.g. `{"providerOptions": {"gateway": {"only": ["anthropic"]}}}`) is merged onto every request body verbatim so a run's evidence names which one it actually spoke to. | Set per run with `uv run capability run <name> --body-extra` or once in the operator's `.env`; recorded in the run's `run.json`. |
| `--effort off` for a non-Anthropic backend | `output_config.effort` is Anthropic-specific; against a backend with no such field it is a 400 waiting to happen. `off` keeps the field off the wire entirely rather than sniffing the model id for provider knowledge the organism should not have. | Set per run with `uv run capability run <name> --effort off` whenever the base URL points anywhere but Anthropic's own API. |

`ANTHROPIC_BASE_URL` selects it, per run with `uv run capability run <name> --base-url` or once in
the operator's `.env`. `hatch.sh` bakes it into the brain key's `relay.targets`, so it must not move
between hatch and run.

`OPENAI_API_KEY` is unaffected: mem0's embedder is an OpenAI SDK call that never touches the relay
or the gateway.

## Accounts and secrets the host setup needs

| Item | Purpose | Status |
|---|---|---|
| Cloudflare DNS: `mshkn.dev` and `*.mshkn.dev` A records → host IP | Caddy routes `{port}-{computer_id}.mshkn.dev` to VMs; `api.mshkn.dev` to the orchestrator | Must be repointed to the new host |
| Cloudflare API token with DNS:Edit on `mshkn.dev` | Caddy's DNS-01 challenge for the wildcard certificate | Previously named `mshkn-caddy-dns`; needs the value, or a new token |
| R2 bucket `mshkn-checkpoints` with access key and secret | Checkpoint upload and Litestream replication | Present in the local `.env`; nothing to do unless rotated |
| Operator SSH public key in `/root/.ssh/authorized_keys` | Deploy and E2E scripts | `~/.ssh/id_ed25519.pub` on the dev machine |
| `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` | Real-model runs of the embryo (`embryo/hatch.sh` with `MEMBRANE_MODEL=anthropic`; `uv run capability run hatch`), billed per token. The E2E suite does not need them: Phase 14 runs the scripted model with a local embedder. | In the operator's local `.env` (git-ignored) beside the R2 keys, or in the environment; stored nowhere in this repository. The OpenAI account must carry a credit balance: without one the brain's first memory recall fails with `insufficient_quota` and every turn exits 1 (found 2026-09-09, the first attempt at #101). One hatch embeds a few thousand tokens with `text-embedding-3-small`; a few dollars last for many runs. |

## What the operator provides, in order

1. The rented host's IP address, with root SSH accepting the operator's key.
2. DNS repointed (or a DNS-edit token so it can be scripted).
3. The Caddy DNS-01 token.

Setup then follows `DEPLOY.md` verbatim; `scripts/e2e.sh` with `MSHKN_SERVER=root@<ip>` runs the suite.

## Not sufficient

- The developer laptop: has KVM but would run the orchestrator as root with tap devices and iptables rules on a workstation, and cannot serve the wildcard domain.
- General-purpose cloud VMs without nested virtualization (checked 2026-09-04: two available 4-vCPU, 7 GB hosts have no `/dev/kvm`).
