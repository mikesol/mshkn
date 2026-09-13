# Sleeping computers: idle reaping becomes idle sleeping

Date: 2026-09-13. From the brainstorm on #74 ("Long-lived computers: a
per-computer idle exemption or a service mode"). Replaces all three options that
issue proposed. Depends on #75 (host-side GC) to be safe at rest; files #179 for
the eviction policy it deliberately does not build.

## 1. Why

The reaper checkpoints and destroys every running computer whose last exec is
older than `idle_timeout_seconds` (`reap_idle`, `src/mshkn/services/reaper.py:128`).
That is right for a one-turn-one-computer agent and wrong for anything meant to
keep serving: a website on its public URL, a long job, a listener for webhooks
that are not ingress rules.

#74 proposed three ways to exempt such a computer: a per-computer `keep_alive`,
a `pin`, or a distinct service mode with its own reaper policy. All three add a
thing the tenant has to know about in advance, and all three fail the same way —
the agent that forgot to set the flag discovers it as a 404, after the fact,
with no way back.

This document takes the other road. The reaper already does the expensive,
correct thing: it **checkpoints, then destroys**. What an idle computer loses is
its running process and its route, never its disk. So the fix is not to exempt
anything. It is to stop throwing away the identity.

## 2. What exists today

- `ComputerStatus` (`src/mshkn/models.py:29`) is `creating`, `running`,
  `destroying`, `destroyed`. There is no state between running and gone.
- `reap_idle` (`reaper.py:128`) skips computers in `self.computers.busy` (#108),
  takes `last_exec_at or created_at` as the reference time, and hands each idle
  computer to `_checkpoint_and_destroy` (`reaper.py:162`) under a semaphore.
  That method creates a checkpoint with `CheckpointTrigger.IDLE` and the label
  `auto-idle-timeout` unless the source had one, then calls `destroy`.
- A computer's public URL is `{port}-{computer_id}.{domain}`, a Caddy route
  matched by regex on the computer id and pointed at the VM's IP
  (`src/mshkn/host/caddy.py:44`). The API reports it as
  `https://{computer.id}.{domain}` (`src/mshkn/api/computers.py:228`).
  `add_route(computer_id, vm_ip)` and `remove_route(computer_id)` are both keyed
  on the computer id alone.
- `_bring_up` (`computers.py:282`) mints a fresh `comp-<uuid12>`, acquires a slot
  and volume id from the allocator, snaps the disk, boots or restores, records
  the row, then warms SSH and adds the route. `destroy` (`computers.py:441`) and
  `cleanup_dead` (`computers.py:479`) undo it, releasing the slot.
- `fork` (`computers.py:205`) is `_bring_up` over a checkpoint's
  `thin_volume_id`. It is what a wake needs, except for two things: it mints a
  new id, and it passes `DEFAULT_RESOURCES` (`computers.py:222`).
- `create` refuses when `active_count(account.id) >= account.vm_limit`
  (`computers.py:173`).
- The `computers` table (`migrations/001_initial.sql:14`, amended by 003, 005,
  009 and 012) stores `thin_volume_id`, `tap_device`, `vm_ip`, `socket_path` and
  `firecracker_pid` — every one of them a property of a live VM — and stores no
  memory or vCPU figure at all.

## 3. What was decided

Four questions, four answers, from the brainstorm:

1. **A long-lived computer does not have to stay running.** It may be reaped and
   woken. This is a deliberate constraint: nothing in guest RAM survives, an open
   connection does not survive, and a cron inside the VM does not run while the
   computer sleeps. Workloads that need those are not supported, and saying so is
   cheaper than building for them.
2. **Both the URL and the API wake it.** Sleeping is a genuine state a computer
   can be in, not a web-serving trick. `exec`, `status`, `upload`, `download`
   and `checkpoint` on a sleeping computer wake it first. `destroy` is the only
   thing that ends a computer.
3. **Every computer sleeps.** There is no flag, no pin and no mode. The idle
   reaper stops destroying and starts sleeping, for everyone, always.
4. **A wake that cannot get a slot fails with 503.** The substrate does not make
   room. Evicting a running computer to serve a wake is #179, filed as YAGNI.

Decision 3 is the one that deletes the issue. Once every computer behaves the
way a long-lived one needs to, there is nothing left to exempt.

## 4. The model

`ComputerStatus` gains `sleeping`, between `running` and `destroyed`.

A sleeping row keeps its `id`, `account_id`, `recipe_id`, `api_key_id`,
`created_at` and `last_exec_at`. It keeps `source_checkpoint_id`, repointed at
the checkpoint the sleep produced — that checkpoint is the computer's state, and
it is how the wake restores. It releases everything that described a live VM:
`firecracker_pid` becomes null, and `tap_device`, `vm_ip`, `socket_path` and
`thin_volume_id` are rewritten on each wake rather than trusted from the row.

Two schema additions are required, both because the row must now outlive the VM
it described:

- **`mem_mib` and `vcpu` on `computers`.** Today the resources a computer was
  created with exist only in the running Firecracker process. `fork` already
  loses them, silently, by passing `DEFAULT_RESOURCES`. A wake that did the same
  would hand an agent back a computer quietly smaller than the one it went to
  sleep with. The figures are written at `_bring_up` and read at wake.
- **`slept_at` on `computers`.** The wake path does not need it; #75 does, and
  the alerting does. "How long has this been asleep" is not answerable from
  `last_exec_at`, because a computer can be woken and go idle again without an
  exec ever happening — an HTTP request to its URL is not an exec.

`fork`'s loss of resources is a pre-existing bug that this design surfaces
rather than causes. It is fixed here because the same field fixes both, and
leaving `fork` wrong while `wake` is right would be two behaviours for one
concept.

## 5. The reaper

`_checkpoint_and_destroy` becomes `_checkpoint_and_sleep`. The checkpoint it
takes is unchanged — same `CheckpointTrigger.IDLE`, same `auto-idle-timeout`
label fallback, same `lifecycle.spawn_drain`. What changes is the ending: rather
than `destroy`, it tears down the VM's host resources and marks the row
`sleeping` with `source_checkpoint_id` set to the checkpoint it just took.

Tearing down is the existing `_teardown` pass (`computers.py:508`): kill the
process, remove the route, remove the volume, tear down the tap, release the
slot. All of it is right for a sleep. The only difference from `destroy` is the
final status write, and that the row survives.

Three reaper behaviours are unchanged and must stay unchanged:

- **The dead-VM reaper** (`reap_dead`) still runs. A computer whose Firecracker
  process died without a checkpoint has lost state; it is `destroyed`, as today.
  Sleeping is for the orderly path only.
- **`busy` is still skipped.** A computer with a command running on it is not
  idle (#108).
- **The host-pressure checks** (`check_host`) still run and still alert.

## 6. The wake

`ComputerService.wake(account, computer)` restores the row's
`source_checkpoint_id` into a new slot, keeping the id:

1. Take a per-computer lock, keyed on the computer id. Ten simultaneous requests
   to a sleeping URL must produce one wake, not ten. The lock is held for the
   duration of the restore; the losers wait on it and then re-read the row.
2. Re-check the status under the lock. A computer that woke while we waited is
   already running; a computer somebody destroyed while we waited is a 404.
3. Acquire a slot. On `LimitExceeded` from the allocator, release the lock and
   fail — see §7.
4. Restore, exactly as `fork` does: snap the checkpoint's `thin_volume_id`,
   restore the snapshot files, warm SSH, `add_route(computer.id, new_vm_ip)`.
   Resources come from the row's `mem_mib` and `vcpu`, not from
   `DEFAULT_RESOURCES`.
5. Write the row: `running`, new `thin_volume_id`, `tap_device`, `vm_ip`,
   `socket_path`, `firecracker_pid`; `slept_at` cleared.

Because `add_route` is keyed on the computer id, the URL is identical before and
after. Nothing outside mshkn observes the wake except as latency.

`get_running` (`computers.py:141`) is the single choke point for every API call
that needs a live VM, and it currently raises `BadRequest` for any non-running
status. It becomes the wake trigger: on `sleeping`, wake and return; on anything
else, behave as today. That is what makes decision 2 one change rather than
fifteen.

The HTTP path is the same call from the other direction. Caddy's route for a
sleeping computer does not exist, so a request to its URL reaches whatever
handles unmatched hosts. A fallback route to the orchestrator, matching
`*.{domain}`, resolves the computer id from the Host header, wakes it and then
proxies. This is the only genuinely new plumbing in the design.

## 7. Capacity

A wake needs a slot and the RAM behind it. Both are finite, and after decision 3
the population of wakeable computers is not.

When the allocator has nothing to give, the wake fails: `503` with a
`Retry-After`, and a log line at warning level naming the computer that could not
be woken. The API call gets the same 503 rather than a `BadRequest`, because the
request is not wrong — the host is full.

This is the honest answer and it is one branch of code. It also gives an operator
a signal they can act on: add capacity, or raise `idle_timeout_seconds` so fewer
computers are asleep in the first place. The alternative, evicting a running
computer to make room, is #179: it makes one tenant's latency a function of
another tenant's traffic, turns the reaper into an admission controller, and is
very hard to take back once agents depend on the current behaviour.

`vm_limit` is now a limit on **running** computers. `active_count`
(`computers.py:149`, over `count_active_computers_by_account`) must exclude
`sleeping`, because a sleeping computer holds no slot, no tap, no RAM and no
Firecracker process — charging an account for it would contradict the "$0 sleep"
claim the product is built on. The consequence is that an account can hold an
unbounded number of sleeping computers, which is why this design is not safe
without #75.

## 8. What this does not do

- **It does not collect anything.** Sleeping computers accumulate: a row, a
  checkpoint and its blobs, for every computer anyone ever created. #75 is the
  prerequisite for running this in anger, and it is now a harder problem than it
  was, because a sleeping computer is no longer obviously garbage. Whatever #75
  decides, it needs `slept_at`.
- **It does not evict.** #179.
- **It does not add a per-computer timeout.** `idle_timeout_seconds` stays
  global. The reason #74 wanted a per-computer one was to avoid being reaped, and
  being reaped no longer means being lost.
- **It does not preserve anything in RAM.** Decision 1.
- **It does not make sleeping free.** A sleeping computer costs storage for its
  checkpoint. It costs no compute.

## 9. Risks

- **The Caddy fallback route is the weak point.** It sees every request for an
  unknown host in the domain, including typos and scans. It must resolve the id
  against the database and 404 fast on a miss, and it must not become a way to
  probe which computer ids exist — a 404 for "no such computer" and a 404 for
  "not yours" have to be indistinguishable.
- **Wake latency is restore latency**, which the Phase 1 latency tests already
  measure for fork. A website that sleeps between visitors pays it on every first
  visit. That is the deliberate constraint of decision 1, but it should be
  measured and stated, not discovered.
- **A wake storm after a restart.** `Runtime.start` recovers state; if many
  sleeping computers are woken at once by traffic, the allocator is the only
  thing rationing them. 503 is the correct answer and it will be visible.
- **`sleeping` is a new status that old code does not know about.** Every place
  that branches on `ComputerStatus` has to be found and told. `get_running` is
  the one that matters; the rest are the reaper, the metrics gauge and the
  startup recovery.

## 10. Tests

Unit, against the fake host:

- The idle reaper sleeps rather than destroys: row survives, status is
  `sleeping`, slot is released, `source_checkpoint_id` names the checkpoint it
  just took.
- The dead-VM reaper still destroys. A VM whose process died is not put to sleep.
- A busy computer is not slept.
- `get_running` on a sleeping computer wakes it and returns a running one with
  the same id.
- Wake restores the recorded `mem_mib` and `vcpu`, not the defaults. This is the
  test that pins the `fork` bug too.
- Ten concurrent wakes of one computer produce one restore.
- A wake with no free slot raises, and the row is still `sleeping` afterwards —
  a failed wake must not strand a computer in a state nothing can recover.
- `active_count` excludes sleeping computers; an account at `vm_limit` whose
  computers are all asleep can still create.

Flow, against the real ASGI app:

- `POST /computers`, sleep it by hand, `POST /computers/{id}/exec` — the exec
  succeeds and the URL reported by `GET /computers/{id}` is unchanged across the
  sleep.

E2E, against a live server:

- A computer serving HTTP, slept, still answers on the same URL after the idle
  timeout, with the cold-start latency recorded.

The negative control for all of it: stash `src/`, confirm the reaper tests fail
with a `destroyed` row rather than a `sleeping` one, and that the wake tests fail
with `BadRequest: Computer is destroyed`.
