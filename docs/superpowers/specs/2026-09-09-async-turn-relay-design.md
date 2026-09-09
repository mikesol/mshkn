# Asynchronous turns through a host-side relay

Date: 2026-09-09. From the brainstorm on #110 (spec-change), which decided to fold `lampas` (https://lampas.dev, the operator's request-to-webhook relay) into mshkn as a host-side service. This document changes the embryo's turn protocol (spec §6 of `docs/superpowers/specs/2026-09-08-embryo-design.md`) and adds one service, one table, two routes and one scope to mshkn. The measure (#101, #111) resumes on it.

## 1. Why

A turn of the embryo is one fork of `brain` whose exec is one membrane command. mshkn gives a fork's exec 300 s and the membrane stops at 240 to save its state. Eight runs against `claude-opus-5` on 2026-09-09 (`docs/embryo/README.md`) show that at the API's default effort the model spends whole turns thinking on the hard turns and proposes nothing; follow-ups repeat it; no run reached more than 3 of 7 postconditions. The turn's clock and the model's deliberation are not the same size, and no budget or margin changes that.

The change: a turn stops being one VM lifetime. The brain composes the model request, hands it to a relay on the host with its own wake-up as the delivery, saves the in-flight turn in `state.json`, acknowledges, and self-destructs. The relay calls the model for as long as it takes and then forks `brain` with `membrane resume <job_id>`; that fork appends the answer, runs the tool calls, and either posts the next request and exits or ends the turn. A conversational turn is a chain of forks; the model's deliberation is bounded by the relay's patience, not by a VM's clock.

Lampas already did the relay's half as a Cloudflare Worker: `POST /forward` returns a job id at once, makes the upstream call, and posts an envelope to each callback with exponential retry, with the forwarded headers stored apart and deleted after the call. It has one user and no purpose apart from a system like mshkn; mshkn cannot host a thinking agent without something like it. Its job schema, retry policy, SSRF guard and envelope are the reference for what follows; its deployment retires once the relay is in.

## 2. Decisions

| # | Question | Decision |
|---|---|---|
| 1 | Where does the relay live? | In mshkn: `mshkn.services.relay.RelayService`, table `relay_jobs`, routes `POST /relay` and `GET /relay/{job_id}`, scope `relay`. Decided on the issue. |
| 2 | How is the wake-up authorized? | Pinned in the scoped key's scope, not chosen by the job. A leaked brain key can make the host run `membrane resume <job_id>` on `brain` and nothing else, and `resume` ignores any id that is not its pending job. §10.2 holds. |
| 3 | How does delivery reach the brain? | The relay forks the pinned label with the pinned exec plus the job id, `self_destruct`, `defer_on_conflict`, through the same admission every fork uses. No HTTP hop, no ingress rule. |
| 4 | Where is the model key? | On the brain until #92, forwarded per job as `forward_headers`, stored apart from the job and deleted the moment the upstream call settles. #91 and #92 replace the forwarded headers with a secret name; the relay is their host-side home. |
| 5 | How does a public principal read a reply? | Root's `list`, for now. The door's synchronous answer is the acknowledgement. A callback root names is deferred (§11). |
| 6 | A `say` while a turn is pending? | Queued with its principal, acknowledged as queued, and run next by the fork that closes the pending turn. Nothing waits on a further knock. |
| 7 | How do the deterministic tiers prove the asynchronous turn? | Through the real relay. `membrane serve` speaks the Messages API wire format for the scripted model; the flow tier mounts it in process and the live tier runs it on a computer behind its Caddy route. |
| 8 | Upstream retry? | Yes, with the job's policy, on the failures the SDK itself retries. A deviation from lampas, which retried delivery only (§10). |
| 9 | Streamed responses? | The relay reassembles a `text/event-stream` body into the final message, keeping every block. Lampas kept text deltas only. |

## 3. The relay service

`mshkn.services.relay.RelayService`, wired in `Runtime.build` after `Lifecycle` (it forks through `CheckpointService` and runs through `Lifecycle`), with the shared `httpx.AsyncClient` (`Runtime.http`, the one callbacks use, so tests can hand it an in-process transport) and `BackgroundTasks`.

### The job

A job is one upstream HTTP call plus at most one delivery.

```json
{
  "target": "https://api.anthropic.com/v1/messages",
  "method": "POST",
  "forward_headers": {"x-api-key": "…", "anthropic-version": "2023-06-01"},
  "body": {"model": "claude-opus-5", "max_tokens": 64000, "stream": true, "…": "…"},
  "retry": {"attempts": 3, "backoff": "exponential", "initial_delay_ms": 1000, "max_delay_ms": 30000},
  "timeout_seconds": 3600,
  "deliver": {"label": "brain", "exec": "membrane resume"}
}
```

| Field | Meaning |
|---|---|
| `target` | The URL to call. Must pass the SSRF guard. Required. |
| `method` | `GET`, `POST`, `PUT`, `PATCH`, `DELETE` or `HEAD`; default `POST`. |
| `forward_headers` | Sent on the upstream request and nowhere else. Stored in their own column, never returned by any route, deleted when the call settles whatever its outcome. Default none. |
| `body` | Any JSON value. A string is sent as it is; anything else is JSON-encoded with `content-type: application/json` unless `forward_headers` sets one. Bounded by `relay_body_bytes`. `GET` and `HEAD` send none. |
| `retry` | Lampas's policy and defaults: attempts (at least 1), exponential backoff, `min(initial_delay_ms * 2^attempt, max_delay_ms)`. |
| `timeout_seconds` | The whole upstream call, connect to last byte, per attempt. Default and cap `relay_timeout_seconds`. |
| `deliver` | The wake-up: the label to fork and the exec to run, with the job id appended as its last argument. Account key: optional; absent means the job is polled and nothing is forked. Scoped key: forbidden in the body (422), the scope pins it (§4). |

Statuses: `queued` (accepted), `in_progress` (an attempt is running), `completed` (an upstream response was received, whatever its status code), `failed` (no response after the attempts, or a final failure). `attempts` counts upstream attempts. `error` names the failure. A job has `created_at` and `updated_at`.

### The upstream call

Runs in a `BackgroundTasks` task named `relay:<job_id>`. Retryable failures, with the job's policy: a transport error, a `408`, `409`, `429` or any `5xx` status (`529` included), or in a streamed body an `error` event of type `overloaded_error` or `api_error`. Final at once: a timeout (retrying would multiply the wait), a response body over `relay_body_bytes`, any other `error` event, any other status (stored as `completed` with that status; the caller reads the error body). When the attempts are spent the job is `failed` with the last error.

The response is stored whole: `status`, `headers` (verbatim), `body`. When the upstream `content-type` is `text/event-stream`, the body is read to the end and reassembled into the final message, and the reassembled JSON is what is stored: `message_start` gives the message with empty `content`; each `content_block_start` inserts its block at its `index`; `text_delta` appends to `text`, `thinking_delta` to `thinking`, `signature_delta` sets `signature`, `input_json_delta` accumulates a partial JSON string parsed into `input` at `content_block_stop` (an empty accumulation is `{}`); `message_delta` merges its `delta` (`stop_reason`, `stop_sequence`) into the message and its `usage` fields over the message's (they are cumulative); `ping` and unknown event or delta types are ignored; `message_stop` ends the message. A stream that ends before `message_stop` without an `error` event is a transport error. Any other body is stored verbatim, parsed as JSON when the content type says so.

### The SSRF guard

Ported from lampas (`packages/core/src/ssrf.ts`, `packages/cloudflare/src/ssrf-guard.ts`) with its tests: `http` and `https` only; a raw address in a blocked range is refused; a hostname is resolved (A and AAAA, through the process's resolver, injectable on the service for the flow tier) and every address is checked; IPv4-mapped IPv6 is unwrapped. Blocked ranges: `0.0.0.0/8`, `10.0.0.0/8`, `127.0.0.0/8`, `169.254.0.0/16`, `172.16.0.0/12` (the VM and Docker ranges), `192.168.0.0/16`, `::1/128`, `fc00::/7`, `fe80::/10`. Two deviations from lampas: a hostname that does not resolve is refused, not allowed (lampas failed open behind Cloudflare's own fetch guard; mshkn has none), and there is no allowlist or off switch (the flow tier injects a resolver instead). The guard runs at `POST /relay` and answers 422 naming the reason. Addresses are not re-resolved at connect time; DNS rebinding is a known gap (§11).

### Bounds, restart, retention

`relay_body_bytes` (default 8 MiB) bounds the request `body` as serialized (413 at `POST /relay`) and the bytes read from an upstream response (a final failure). `relay_timeout_seconds` (default 3600) is the default and the cap of `timeout_seconds`. Both are `Config` fields and `MSHKN_RELAY_BODY_BYTES` / `MSHKN_RELAY_TIMEOUT_SECONDS`.

A restart re-runs every job still `queued` or `in_progress` (`Runtime.start` calls `RelayService.resume`): their headers are still stored, the brain has seen nothing, and the cost of a repeated model call is smaller than a turn that never closes. The reaper expires jobs, their responses included, with `exec_log_retention_seconds`, in the same cycle as exec logs.

## 4. Routes and scope

| Route | Account key | Scoped key |
|---|---|---|
| `POST /relay` | always; `deliver` optional | `relay` scope required; `target` must start with one of `relay.targets`; `deliver` must be absent from the body and is taken from the scope. The job records the key. |
| `GET /relay/{job_id}` | any job on the account | only a job this key created; 404 otherwise, as computers |

`POST /relay` answers 202 `{"job_id": "rj-<12 hex>", "status": "queued"}`.

`GET /relay/{job_id}` answers:

```json
{
  "job_id": "rj-…", "status": "completed", "target": "…", "method": "POST",
  "created_at": "…", "updated_at": "…", "attempts": 1, "error": null,
  "response": {"status": 200, "headers": {"…": "…"}, "body": {"…": "…"}},
  "delivery": {"label": "brain", "exec": "membrane resume", "status": "delivered",
               "attempts": 1, "computer_id": "…", "deferred_id": null, "error": null}
}
```

`response` is null until `completed`; `delivery` is null when the job has none. `forward_headers` appear in no response, ever.

The scope document (`mshkn.models.Scopes`, `parse_scopes`) gains one section. It is rejected as unknown today, so no existing key changes meaning:

```json
{"relay": {"targets": ["https://api.anthropic.com/"], "deliver": {"label": "brain", "exec": "membrane resume"}}}
```

`targets` is a non-empty list of URL prefixes. `deliver` is the one wake-up the key may cause. The brain's key at hatch becomes `recipes.create`, `recipes.read`, `computers.create_from: "*"`, `labels: ["verb/"]` and this section. `deliver.label` is not required to fall under `labels`, and must not be: that is the point. A scoped key cannot fork `brain` through `POST /checkpoints/fork` (its labels do not cover it), and through the relay it can only cause the pinned command.

## 5. Delivery

When the upstream call settles, `completed` or `failed`, and the job has a delivery, the relay forks: `CheckpointService.fork_by_label(account, deliver.label, ExecSpec(command=f"{deliver.exec} {job_id}", self_destruct=True, callback_url=None, label=None, meta_exec=None), exclusive="defer_on_conflict", recipe_id=None)`, then `Lifecycle.run_ephemeral` for the computer, as the ingress fork action does. The fork is the account's, like the deferred drain's; no key is recorded on the computer. A busy chain answers `Deferred`: the wake-up is queued and the drain runs it when the running fork self-destructs, and the delivery records the deferred id.

Delivery statuses: `pending`, `delivered` (a computer ran it, or the deferred queue holds it), `failed`. A fork that raises (`NotFound` when the chain is gone, `LimitExceeded`, a `HostError`) is retried with the job's policy; after that the delivery is `failed` and the job stays `completed`. A failed delivery never loses the upstream result: the brain's next command settles a completed job on its own (§6). The exec log of the wake-up's computer holds the turn's audit line, so §10.5 of the embryo spec holds across the chain of forks.

## 6. The membrane's turn

This section replaces §6 of the embryo spec from "A `say` turn, in order" on; the doors and the root commands stand, with two additions: `membrane resume <job_id>`, and `list` showing the pending turn, the queue and the window's replies.

### State

`state.json` (#100, one atomic document) gains two fields.

`pending`, the in-flight turn or null:

| Field | Meaning |
|---|---|
| `turn`, `principal`, `door`, `message`, `payload` | The turn as `say` began it. |
| `messages` | The list sent to the model so far: the window's history, the composed input, then each assistant content and its tool results as they happen. |
| `offered` | The tool names offered at the turn's start (§6 step 4). Recorded for the audit line; the handlers are rebuilt on every fork from the current policy and catalog, so a verb approved while the model thought is usable on the next request, and a verb disabled meanwhile answers an error. |
| `job` | The relay job id of the request in flight. |
| `calls`, `model_calls`, `usage`, `hooks`, `made`, `forks`, `started_at` | The turn's bookkeeping: tool calls run, model calls made, summed usage, the hooks' runs, proposals made, forks taken, when it began. |
| `memory_written` | Whether the principal is authenticated, decided at the start. |

`queue`, the messages that arrived while a turn was pending: each `{principal, door, message, payload}` with the hooks already run.

### `say`

Steps 1 to 4 of the embryo spec's §6 run as today: principal (hooks on the public door), builds and trials polled, inbox drained and memory recalled for an authenticated principal, tools decided. Then, instead of the loop: the membrane composes the request the model module used to send (`model`, `max_tokens` 64000, `stream: true`, `system`, `messages`, `tools`, and `output_config.effort` when set), posts it to `POST /relay` with `target` `<ANTHROPIC_BASE_URL>/v1/messages` and the key and version as `forward_headers`, stores `pending`, prints one audit line (`turn`, `principal`, `door`, `hooks`, `offered`, `job`, `started: true`) and the acknowledgement `{"turn": N, "job": "rj-…"}`, saves, exits 0. No model call happens inside a `say`. A closed door and a bad payload answer as today. A `say` that finds `pending` set runs the hooks, appends to `queue`, prints the audit line and `{"queued": position}`, and exits 0.

### `resume <job_id>`

The wake-up, and the only membrane command the relay may run. If `pending` is null or its `job` is not `job_id`, it prints one line saying so and exits 0: a forged or stale wake-up does nothing. Otherwise it fetches `GET /relay/{job_id}` and continues the turn:

- `in_progress` or `queued`: nothing (a wake-up arrived before the job settled cannot happen, but the command is safe).
- `failed`, or `completed` with a status outside 2xx: the turn ends with the error as the reply and `stopped: error`.
- `completed` with a message: parsed as `AnthropicModel.complete` parses one today (text blocks, `tool_use` blocks, `parsed_output` stripped from the echoed content). `max_tokens` ends the turn with `stopped: max_tokens`, its calls not run. No calls ends it with the text, `stopped: done`. Calls run through the handlers, each bounded by this fork's clock (`TURN_DEADLINE`, 240 s from the fork's start, as tool runs are today); a call past the cap (20 per turn) ends the turn with `stopped: cap`. Their results are appended to `messages`, the next request is posted, `pending.job` becomes the new id, a short audit line records the fork (`turn`, `job`, `calls`, `next_job`), and the fork exits 0.

Ending the turn is today's step 6: memory written for an authenticated principal, the exchange appended to the window with its reply, the full audit line (today's fields plus `forks`, `started_at` and `job`), then the reply and the proposals made, on stdout. The window entry carries the reply, the printed output (the reply with the proposals) and the closing audit fields, so `list` shows a turn that closed in a fork nobody watched, and the measure reads the audit from there.

If `queue` is non-empty when a turn ends, the same fork starts the next turn from its head: the queued principal and door stand (the hooks ran when it was queued), steps 2 to 4 run for it, its request is posted, and its start audit line and acknowledgement are printed after the ended turn's output.

### Settling

Every root command and every `say` first settles a pending turn, before anything else: it asks `GET /relay/{job}`; `completed` or `failed` runs the resume inline, exactly as the wake-up would; `in_progress` leaves it. So a wake-up whose fork failed, or a job the relay timed out, ends on the next command instead of never; the relay's `timeout_seconds` is the turn's outer bound.

### `list`

Adds `pending` (turn, principal, door, job, started_at, forks, model_calls, usage), `queue` (principal, door, message each), and `window` (the exchanges with their replies, outputs and closing audits).

### What the brain cannot do, by construction

Unchanged from the embryo spec, and one thing more: it cannot make the host call anything but the prefixes its key names, and cannot make the host run anything on `brain` but its own `resume`.

## 7. The scripted path, hatching, and the tiers

### `membrane serve`

`membrane serve [--port 8000]` answers `POST /v1/messages` from the `ScriptedModel` on the Messages API wire format, with the standard library's HTTP server and no new dependency. It reads `system`, `messages` and `tools` from the request body, calls the scripted model, and answers a plain JSON message: `content` as the scripted completion's content, `stop_reason` `tool_use` when it carries calls and `end_turn` otherwise, `usage` zeros. `stream` is ignored; the relay stores the JSON verbatim. The handler is one function over the body so the flow tier can mount it as an ASGI app. It refuses to start unless `MEMBRANE_MODEL=scripted`.

### `.env` and hatching

`.env` gains `ANTHROPIC_BASE_URL` (default `https://api.anthropic.com`); `MEMBRANE_MODEL=scripted` no longer requires the model keys and no longer makes the membrane answer in process. `hatch.sh` mints the brain's key with the `relay` section, and in scripted mode, before the `brain` checkpoint is taken, creates a second computer from the brain recipe, installs the wheel, starts `membrane serve` with `POST /computers/{id}/exec_bg`, and writes its route (`https://8000-<computer id>.<domain>`) into the brain's `.env` as the base URL. It prints that computer's id beside the rest. The idle reaper does not count a background process as activity (§12), so whoever hatches in scripted mode keeps the server alive: the live tier touches it between phases, and tears it down with the rest.

### Flow tier

`tests/flow/test_embryo_liturgy.py` keeps its shape: the membrane in process against the real app over the fake host. Its transport routes `http://model/v1/messages` to the serve handler, the relay's resolver is injected to resolve that name to a public address, and the brain's `.env` names it. Each door call now waits: after a `say`, the test drains background tasks (the relay's call and its delivery fork run there) until `list` shows no pending turn, then reads the reply from the window. The liturgy's assertions do not change.

### Live tier

Phase 14 rewritten to wait for turns through `list` (`Doors.root_say` and `public_say` return the acknowledgement and then poll `list` until the turn is in the window), plus a new Phase 15 for the relay itself (§8). The measure driver (`embryo/membrane/measure.py`) changes the same way and nothing else: its transcript records the reply from `list`, and its command record keeps every `list` it sent, wake-up polls included.

## 8. Proof

**Unit** (`tests/unit/test_relay*.py`, `tests/unit/test_scopes.py`, `tests/unit/test_embryo_*.py`): job validation and defaults; the SSRF guard with lampas's cases ported and the two deviations pinned; the retry policy's delays and the retryable set; SSE reassembly of text, tool_use with chunked partial JSON, thinking with signature, cumulative usage, an `error` event of each kind, a stream cut before `message_stop`; body caps on both sides; headers deleted on every outcome; restart re-run; expiry; delivery retry and its statuses; the `relay` scope parsed and refused (missing, empty targets, malformed deliver); a scoped key held to its prefixes and forbidden a body `deliver`. Membrane: `pending` and `queue` round-trip through `state.json`; a `say` acknowledges and makes no model call; `resume` on each job outcome; a forged id does nothing; the cap across forks; a disabled verb between forks; the queue runs next; settling on `list`; `serve` answers the wire format for a text reply and a tool call.

**Flow** (`tests/flow/test_relay.py`, `tests/flow/test_embryo_liturgy.py`): a job from a scoped key delivered by fork to a labelled chain, its exec log holding the job id, and the response readable by the key that made it and not by another; a delivery deferred behind a running fork and run by the drain; a `failed` upstream still delivered; the liturgy end to end through the real relay; a `say` while a turn is pending, queued and run next, both replies in the window in order.

**Live** (`tests/e2e/test_phase15_relay.py`, `tests/e2e/test_phase14_embryo.py`), added to the test plan as Phase 15 "The Relay":

- T15.1 A job to a public target is delivered to a chain: a labelled checkpoint, `POST /relay` with `GET https://example.com/` and a `deliver` whose exec echoes its argument, `GET /relay/{id}` until `delivered`, the chain one checkpoint longer, the computer's exec log holding the job id, the stored body holding "Example Domain".
- T15.2 The guard refuses the host: `http://127.0.0.1:8000/health`, `http://172.16.254.1/`, `http://localhost/` and `http://[::1]/` are 422 and no job exists.
- T15.3 A scoped key is held to its scope: without the section 403; with it, a target outside the prefixes 403, a body `deliver` 422, a job delivered through the pinned exec, and the job 404 to another key.
- T15.4 Delivery waits its turn: the chain forked with a sleeping exec, a job to a fast target, the delivery `delivered` with a deferred id, and after the sleeper self-destructs the chain two checkpoints longer, in order.

The E2E suite grows by four to 180; the expected line in `CLAUDE.md` and `README.md` becomes 170 passed, 6 skipped, 4 failed, the four still #65's.

## 9. Documents

- The embryo spec: §6 replaced by a pointer to §6 here; §4's `timeout_seconds` row and §15's exec-budget fact reworded to say the fork budget bounds tool runs only; §13 gains the reply callback.
- `docs/ARCHITECTURE.md`: the relay in §4's table; the two routes in §1a's table; `relay_jobs` in §5; a "Relay" section after §9 with the job, the guard, delivery and restart; the two config values in §13; the docs test enforces the routes and variables.
- The test plan: Phase 15, and Phase 14's descriptions restated for the asynchronous turn.
- `README.md`: the relay under "What exists", the counts. `docs/plans/README.md`: a section for this spec and its plan. `embryo/README.md`: the turn is a chain of forks; `serve`; the `.env` variable.
- `docs/infrastructure.md`: unchanged.

## 10. Deviations from lampas, recorded

- One delivery per job, a fork by label rather than a list of URLs. The only caller has one destination and the host can fork directly.
- Upstream retry with the job's policy on the failures the SDK retries. Lampas retried delivery only.
- `timeout_seconds` instead of `timeout_ms`, uncapped below the host's `relay_timeout_seconds`, because mshkn's other timeouts are seconds and the point is a long wait.
- `completed` means a response was received, whatever its status; lampas's envelope called a non-2xx `failed`. The status code is stored beside the body and the caller decides.
- Streamed bodies are reassembled with every block kept; lampas kept text deltas.
- A hostname that does not resolve is refused; no allowlist or off switch.
- No CORS, no public deployment: the routes are authenticated like every other.

## 11. After this change, by name

- **#91, #92**: the vault. `forward_headers` become a secret name the relay resolves; no brain checkpoint holds a model key. The relay is where the key is used.
- **The reply callback**: a URL root names in policy, to which the membrane's reply is delivered through the relay at the end of a turn. Its own issue; needs the invariants to say what an outbound reply is.
- **DNS re-resolution at connect** for the SSRF guard.
- **The measure** (#111), on this turn.
- **Retiring lampas.dev**, once the measure has run on the relay.

## 12. Facts checked before the plan

Checked on 2026-09-09 against the code, the live host and the API documentation. The plan may rely on them.

- **The Messages API does not reject a long non-streaming request; the SDKs do**, as a guard against HTTP timeouts ("the SDKs require streaming to avoid HTTP timeouts", the streaming page). The relay sends what it is given; the membrane asks for `stream: true` so a long answer produces bytes throughout, and the relay reassembles it.
- **The stream grammar**: `message_start` with an empty `content`; per block `content_block_start`, `content_block_delta` (`text_delta`, `input_json_delta` as partial JSON strings, `thinking_delta`, `signature_delta` just before the stop), `content_block_stop`; one or more `message_delta` with cumulative `usage`; `message_stop`; `ping` anywhere; an `error` event (`overloaded_error` is the streamed form of a 529); new event types may appear and must be ignored.
- **The host reaches its own public routes.** From the host, `curl https://api.mshkn.dev/health` answers 200 from `65.21.22.161`, the host's own address; `api.mshkn.dev` and the computer routes resolve to it. The scripted server's route is reachable from the relay, and public, so the guard allows it.
- **The idle reaper ignores a background process.** `Reaper.reap_idle` skips a computer only while it is in `ComputerService.busy`, which `exec` and `stream` set and `exec_bg` does not; `exec_bg` touches `last_exec_at` once. A computer running only `membrane serve` is reaped after `idle_timeout_seconds` (1800 on the host) unless touched.
- **`parse_scopes` rejects unknown top-level fields**, so a `relay` section is a pure addition.
- **`Runtime.http` is injectable** and is what callbacks use; the flow conftest already hands it an ASGI transport.
- **The deferred drain batches wake-ups safely.** `Lifecycle.drain_deferred` joins queued execs with newlines and runs them as one command on one fork, so two wake-ups queued behind a running fork run as two sequential `membrane resume` processes, each loading and saving `state.json`.
- **Fork admission is one operation under the label's lock** (`CheckpointService.fork_by_label`, #89), so a wake-up and a root command cannot both fork the same head.
- **A fork's exec runs with the 300 s default** and `TURN_DEADLINE` is 240; both stay, bounding tool runs inside one fork.
