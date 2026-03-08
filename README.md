# mshkn

Computers that fork. Disposable cloud computers for AI agents.

This is an early experiment. The idea: give agents cheap, fast VMs they can checkpoint, fork, and throw away. Like git branches but for running computers.

## What works today

- Create a VM in ~2 seconds (Firecracker microVM, cold boot)
- Run commands over SSH, streamed back as SSE
- Checkpoint a running VM in ~800ms (Firecracker snapshot + dm-thin)
- Fork from a checkpoint in ~1.8 seconds (dm-thin CoW snapshot, cold boot)
- Forked VMs are fully independent — different IPs, different disk state
- Destroy when done, clean up everything

## What doesn't work yet

- Fork uses cold boot instead of snapshot restore (Firecracker snapshot restore requires the same network config as the original, which conflicts with running both concurrently)
- Merge is stubbed out (3-way filesystem merge exists but isn't wired end-to-end)
- Nix capability layers are stubbed out
- No multi-server support — single Hetzner box
- VMManager doesn't recover cleanly from restarts (leftover tap devices, dm-thin volumes)
- No rate limiting, no billing, no multi-tenancy beyond API keys

## Stack

- Firecracker microVMs on bare metal
- dm-thin provisioning for O(1) copy-on-write disk snapshots
- Python 3.12 + FastAPI + asyncio
- SQLite (via aiosqlite)
- Cloudflare R2 for checkpoint storage

## Running locally (you probably can't)

This needs a bare metal Linux box with `/dev/kvm`, Firecracker installed, dm-thin kernel module, and a prepared rootfs. It's not containerizable. See `deploy.sh` for what the server setup looks like.

## Contributing

This is genuinely early and I'd love help. If any of this is interesting to you — the VM orchestration, the snapshot/fork model, making agents better — open an issue or PR. There's no formal process. The code is straightforward Python, tests run with pytest, linting with ruff, types with mypy.

```
uv sync --extra dev
source .venv/bin/activate
pytest
ruff check .
mypy src/
```

## License

MIT
