# mshkn

Disposable cloud computers for AI agents. A computer is a Firecracker microVM that boots in under two seconds from a copy-on-write snapshot of a base disk. You run commands on it, checkpoint it, fork the checkpoint into more computers, merge two forks, and throw everything away. Checkpoints are the durable thing; computers are not.

This is a single-host research system with no users. The API changes without notice.

## What exists

- **Computers.** `POST /computers` boots a VM from the bare base volume or from a recipe's volume, with 256 MiB and 2 vCPUs unless `needs` says otherwise (`{"ram": "512MB", "cores": 2}`). The request can carry an `exec` command to run immediately, `self_destruct` to checkpoint and destroy afterwards, a `callback_url` to be told the result, and a `label` for the checkpoint chain.
- **Exec.** `POST /computers/{computer_id}/exec` streams stdout, stderr and the exit code as server-sent events over SSH. Background commands (`exec/bg`, `exec/logs/{pid}`, `exec/kill/{pid}`), file `upload` and `download`, and `status` with live CPU, memory, disk and process counts.
- **Checkpoints.** `POST /computers/{computer_id}/checkpoint` pauses the VM, writes a Firecracker memory and device snapshot, takes a dm-thin snapshot of the disk, and resumes. The snapshot files upload to R2 in the background. Checkpoints have labels, parents (a DAG), a pin flag, and a per-account retention count.
- **Fork.** `POST /checkpoints/{checkpoint_id}/fork` restores the snapshot on a fresh slot: memory state comes back, the disk is a copy-on-write child. A fork of a 50 MB working set costs the same as a fork of 1 MB.
- **Exclusive chains.** A fork with `exclusive` set either fails while another computer is active on the label (`error_on_conflict`) or is queued (`defer_on_conflict`) and run when that computer self-destructs or is destroyed.
- **Merge.** `POST /checkpoints/{parent_id}/merge` does a three-way filesystem merge of two forks against their parent into a new checkpoint and reports conflicts.
- **Recipes.** `POST /recipes` takes a Dockerfile that starts `FROM mshkn-base`; the image is built, exported and written into a thin volume, and a booted template snapshot is cached so computers from the recipe restore instead of cold-booting.
- **Ingress.** Unauthenticated webhook URLs (`/ingress/{rule_id}`) whose Starlark transform decides whether to create or fork a computer, synchronously or not, with per-rule body-size and rate limits.
- **Reaper.** Dead VMs are cleaned up, idle VMs are checkpointed and destroyed after `MSHKN_IDLE_TIMEOUT` seconds, old checkpoints are pruned, and thin-pool and host RAM pressure raise alerts at `GET /alerts`.
- **Observability.** JSON logs with request ids, Prometheus metrics at `GET /metrics`, subsystem health at `GET /health`.
- **Tenancy.** API keys with a per-account VM limit and an exec rate limit. Accounts are created with `python -m mshkn accounts create`.

`docs/ARCHITECTURE.md` explains how these fit together.

## What does not exist

- More than one host. Slots, taps, thin volumes and the checkpoint directory are local to the machine; a checkpoint cannot be restored on another host.
- Billing, quotas beyond the VM limit, or any notion of a user beyond an API key.
- Retention of exec output after the computer is gone (#58) and an HTTP forwarding endpoint (#59).
- Seven of the 157 end-to-end tests describe workflows that are not implemented and fail on purpose until they are (#65): three phase-10 agent workflows, structured-log and audit-log checks, the checkpoint storage-cost measurement, and the R2 bucket-policy check.

## Layout

```
src/mshkn/
  main.py            ASGI entry point (uvicorn mshkn.main:app)
  app.py             create_app(): routers, request-id middleware, lifespan
  runtime.py         Runtime: config, db, host, services, background tasks
  cli.py             python -m mshkn accounts create|list, migrate
  config.py          Config from MSHKN_<FIELD> environment variables
  models.py          Account, Computer, Checkpoint, Recipe, IngressRule, enums
  errors.py          NotFound, Conflict, BadRequest, InvalidInput, ... HostError
  resources.py       Resources(mem_mib, vcpus) parsing and bounds
  ratelimit.py       sliding-window RateLimiter
  db/                aiosqlite connection, migrations, one module per table
  host/              Hypervisor, BlockStore, Guest, ObjectStore, Proxy protocols;
                     Firecracker, dm-thin, SSH, rclone and Caddy implementations;
                     in-memory fakes in fake.py
  services/          allocator, computers, checkpoints, lifecycle, recipes,
                     ingress, reaper, merge, callback, starlark
  api/               FastAPI routers, request/response schemas, error mapping
  observability/     JSON logging with request ids; metrics and timed()
migrations/          sequential, additive SQL migrations
scripts/             deploy.sh, e2e.sh, build-rootfs.sh, mshkn-pool-up
systemd/             mshkn, mshkn-pool and litestream units
tests/               unit/, flow/ (real app over the fake host), e2e/ (live server)
docs/                ARCHITECTURE.md, infrastructure.md, plans/ (with an index)
```

## Tests

Three tiers. The first two run anywhere; the third needs the live host.

```bash
uv sync
uv run pytest                 # unit + flow tiers; coverage floor 98%
uv run pytest tests/flow      # the real app and services over the fake host
MSHKN_SERVER=root@<ip> scripts/e2e.sh   # pushes, deploys, runs tests/e2e on the live server
```

The E2E suite is the definition of done for the product (`docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md`). It currently reports 144 passed, 6 skipped and 7 failed; the seven are the unimplemented workflows in #65, and anything else failing is a regression.

The full local gate, which is exactly what CI runs:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

To drive the whole stack without a host, build a `mshkn.host.fake.FakeHost` and hand it to `mshkn.runtime.Runtime.build`; `tests/flow/conftest.py` shows the wiring.

## Running it

It needs a bare-metal Linux host with `/dev/kvm`, Firecracker, the dm-thin kernel module, Docker, and a public address for the wildcard domain. `docs/infrastructure.md` states the minimum; `DEPLOY.md` is the fresh-server procedure, executed verbatim on the current host.

## Working on it

`CLAUDE.md` holds the working rules (the gate, the E2E expectation, no merges without authorization). `docs/plans/README.md` indexes every design and implementation plan with its status.

## License

MIT
