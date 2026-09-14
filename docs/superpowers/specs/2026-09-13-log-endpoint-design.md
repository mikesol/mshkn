# A readable log: ECS records and a `GET /logs` endpoint

**Issue:** #65 (T11.2, structured JSON logs)
**Date:** 2026-09-13
**Status:** designed, not implemented

## 1. Why

T11.2 in `tests/e2e/test_phase11_observability.py` asserts that the server emits
structured JSON logs. It fails as `Not implemented`, and the reason it fails is
not that the feature is missing. `src/mshkn/observability/logging.py` has
formatted every record as a single-line JSON object since PR2. What is missing
is a way for the test to see one.

The e2e tier is an `httpx.AsyncClient` pointed at `MSHKN_API_URL`. It has no
shell on the host, no filesystem, no journal. Logs go to stdout, stdout belongs
to whatever supervises the process, and the harness has no route to it. The
assertion has no reachable evidence, so it is not a test — it is a wish.

This design gives it evidence: the records get a schema an outsider recognises,
and a bounded window of them becomes readable over HTTP by the account that
caused them.

## 2. What exists today

- `src/mshkn/observability/logging.py:10` — `request_id_var`, a `ContextVar[str]`
  defaulting to `"-"`.
- `logging.py:15` — `RequestIdFilter`, which stamps `record.request_id` on every
  record that passes through.
- `logging.py:23` — `JSONFormatter`. Emits `timestamp`, `level`, `logger`, `msg`
  and `request_id`, then copies every non-builtin record attribute in verbatim,
  then `exception` as one formatted string. Field names are mshkn's own.
- `logging.py:46` — `configure_logging(level)`. Idempotent via a marker attribute
  on the root logger. Installs one `StreamHandler` on root and reassigns the
  three uvicorn loggers to it.
- `src/mshkn/app.py:33` — `configure_logging()` is called inside `create_app`,
  before the lifespan runs and therefore **before a Runtime exists**.
- `src/mshkn/app.py:50` — the request-id middleware. Reads `x-request-id` or mints
  a uuid, sets the contextvar, resets it in a `finally`, echoes it back on the
  response.
- `src/mshkn/runtime.py` — `Runtime.alerts: deque[Alert]`, bounded by
  `_ALERT_HISTORY_SIZE = 100`, built in `Runtime.build` and served by
  `GET /alerts` at `src/mshkn/api/system.py:78`. The precedent this design copies.
- `src/mshkn/runtime.py` — the module docstring: *"There are no module-level
  mutable globals in mshkn."* A constraint, not a preference.
- `src/mshkn/api/deps.py:23` — `require_principal`. Resolves the bearer to a
  `Principal`: the account key gives `Principal(account=…)`, a scoped key gives
  `Principal(account=…, key=…)`.
- `src/mshkn/api/deps.py:39` — `require_account_key`. The gate on routes a scoped
  key may never call.
- `src/mshkn/api/system.py:1` — *"Unauthenticated system endpoints: health,
  metrics, alerts."*

## 3. What was decided

Three questions were put and answered on 2026-09-13.

1. **The schema is ECS** — the Elastic Common Schema — rather than OpenTelemetry's
   log data model or mshkn's current field names. ECS is the only one of the three
   a stock collector (Vector, Filebeat, Loki, Datadog) ingests with no transform.
   OTel was the close second and lost on transport: OTLP is a push protocol, and a
   `GET` returning OTel-shaped JSON is a hybrid nobody ships.
2. **The window is an in-memory ring buffer**, not `journalctl` and not a file
   mshkn owns. Shipping and retaining logs is the operator's job; a container
   deployment has no journal at all; and a file makes mshkn responsible for
   rotation and disk accounting. The endpoint exists to verify the recent past,
   not to archive it. stdout stays the durable path.
3. **The endpoint is authenticated and scoped to one account.** `/health`,
   `/metrics` and `/alerts` are unauthenticated because they carry host-level
   facts. Logs carry per-tenant identifiers, and publishing them would turn the
   phase-8 isolation boundary into a public feed.

## 4. The record

One function does the mapping:

```python
def to_ecs(record: logging.LogRecord) -> dict[str, object]: ...
```

Two consumers. The stream handler's formatter calls it and `json.dumps` the
result; the buffer handler calls it and keeps the dict. There is exactly one
schema, and the bytes on stdout and the bytes in the response body are the same
shape. This is what "the same format on input and output" means in practice.

The mapping, from what `JSONFormatter` emits today:

| today | ECS | note |
|---|---|---|
| `timestamp` | `@timestamp` | ISO 8601, UTC, unchanged in value |
| `level` | `log.level` | lowercase, unchanged in value |
| `logger` | `log.logger` | |
| `msg` | `message` | |
| `request_id` | `trace.id` | ECS's own field for correlating one request |
| `exception` | `error.type`, `error.message`, `error.stack_trace` | three fields, not one blob |
| — | `ecs.version` | `"8.11.0"`. Required by ECS; it is what tells a collector which schema this is |
| any `extra=` attribute | `mshkn.<name>` | ECS reserves the top level for itself and puts vendor fields under their own key |

Two details that are decisions, not accidents:

**Dotted keys, not nested objects.** ECS permits both. Dotted is what Filebeat
emits, it is trivial to produce from a flat dict, and it is trivial to assert on
in a test. `{"log.level": "info"}`, not `{"log": {"level": "info"}}`.

**`trace.id` is omitted when there is no request.** Today `request_id_var`
defaults to `"-"` and the field is always present. ECS would rather a field be
absent than carry a sentinel, and a reaper cycle genuinely has no trace. The
contextvar keeps its `"-"` default; `to_ecs` drops the field when it sees it.

The `extra=` path is what carries `mshkn.computer_id` and
`mshkn.checkpoint_id`. No call site changes: `logger.info(…, extra={"computer_id":
cid})` already produces a `computer_id` record attribute today, and today's
formatter already copies it in. Only the key it lands under changes.

## 5. The account

`GET /logs` returns the caller's records and no one else's, so every record needs
to know which account caused it.

Add `account_id_var: ContextVar[str | None]` beside `request_id_var` at
`logging.py:10`, and set it inside `require_principal` (`deps.py:23`) once the
account resolves. The filter stamps it the way `RequestIdFilter` stamps the
request id; `to_ecs` puts it at `mshkn.account_id`.

Nothing needs resetting. Contextvars are task-local, and Starlette gives each
request its own task — the same property the request-id middleware already relies
on. A background task spawned during a request inherits the account, which is
what you want: a teardown kicked off by a destroy call belongs to the account
that called destroy.

It cannot be set in the middleware at `app.py:50`, because authentication is a
route dependency and runs after the middleware has already handed control down.
That has a visible consequence; see §7.

## 6. The endpoint

A new module, `src/mshkn/api/logs.py`. Not `system.py`: that file's first line
says "Unauthenticated system endpoints", and this one is not.

```
GET /logs?limit=100&since=2026-09-13T10:00:00Z
```

- **Auth:** `require_account_key` (`deps.py:39`). A scoped key gets 403.
- **Response:** `application/x-ndjson`, one ECS object per line, oldest first.
- **`since`:** an ISO 8601 timestamp, exclusive. Records with an `@timestamp`
  at or before it are dropped.
- **Filter:** only records whose `mshkn.account_id` equals the caller's account.
- **`limit`:** default 100, clamped to the buffer size. Applied last, and it
  keeps the **newest** `limit` records of whatever survived the filter and
  `since` — a truncated response drops the oldest end, never the most recent
  thing that happened. The surviving records are then written oldest first, so a
  caller polling with `since` reads a continuous stream.

The account key rather than any key is a deliberate narrowing. A scoped key is a
delegation with fewer powers than the account key — it may only create from
certain recipes, only touch labels under its prefixes, only reach computers it
created (`src/mshkn/api/scopes.py:60`). Handing it the account's entire log
stream would hand it a view of everything those restrictions exist to hide.
Merge and recipe-delete already take this position; `/logs` joins them.

### Wiring the buffer

`configure_logging()` runs at `app.py:33`, before a Runtime exists, so the buffer
cannot be created inside it. And it cannot be a module-level global, because
`runtime.py` forbids those.

So the Runtime owns it, exactly as it owns `alerts`:

- `Config` gains `log_buffer_size: int = 1000`. The generic `MSHKN_<FIELD>` rule
  in `Config.from_env` picks up `MSHKN_LOG_BUFFER_SIZE` with no extra code.
- `Runtime` gains `logs: deque[dict[str, object]]`, built in `Runtime.build` with
  `maxlen=config.log_buffer_size`.
- `install_log_buffer(logs)` adds a `logging.Handler` to the root logger that
  appends `to_ecs(record)` to the deque. It removes any handler of its own type
  first, so a second call replaces rather than doubles.
- The lifespan in `create_app` calls it after the Runtime is built and before
  `rt.start()`.

The flow tier builds a Runtime, so it gets the buffer for free and can assert on
the endpoint against real records.

## 7. What this does not do

**No streaming and no follow.** No SSE, no `?follow=1`, no long poll. A caller
that wants a live tail polls with `since`.

**Nothing survives a restart.** The buffer is memory. stdout is the durable path,
and whatever collects stdout now collects ECS.

**It is not the audit log.** #180 is a durable, complete record of create,
destroy, checkpoint and fork, in SQLite, answerable months later. This is a
bounded window over whatever the process happened to log. They are different
artifacts and neither substitutes for the other.

**A tenant will not see uvicorn's access lines.** Those are emitted by
`uvicorn.access` before authentication has run, so they carry no account and the
filter in §6 drops them. What a tenant sees is mshkn's own application records —
which is what T11.2 needs and what a tenant debugging their own computer wants.
The access lines remain on stdout for the operator. Fixing this would mean
resolving the bearer twice per request, once in middleware and once in the
dependency, and it is not worth a doubled auth lookup on every call.

**One buffer per process.** If mshkn is ever run with more than one uvicorn
worker, each worker has its own buffer and `GET /logs` answers from whichever
worker took the request. It runs as a single process today and the deployment in
`DEPLOY.md` does not change that, but the endpoint is not correct under
multi-worker and should not be assumed to be.

## 8. Risks

**A record's size is unbounded.** The ring bounds the number of records, not
their bytes. A single `extra=` carrying something large sits in memory until it
is evicted. Mitigation: `to_ecs` truncates any stringified value past 4 KiB and
marks it. Cheap, and it keeps the worst case at `log_buffer_size × 4 KiB` per
field rather than unbounded.

**Renaming every field breaks anything already parsing the old names.** mshkn is
pre-alpha and there is no compatibility obligation, so the old names go and
nothing is kept alongside them. The one consumer inside the repo is
`tests/unit/test_observability.py`, which is rewritten with the change.

**Leaking across accounts is the failure that matters.** A bug in the filter
hands one tenant another's activity. The flow tier gets a test that is
specifically about this — two accounts, each seeing only its own records — and it
is the test to look at first if the filter is ever touched.

## 9. Tests

**Unit** (`tests/unit/test_observability.py`, rewritten):

- Each mapped field: `@timestamp`, `log.level`, `log.logger`, `message`,
  `ecs.version`.
- `trace.id` present when the contextvar is set, absent when it is `"-"`.
- An exception becomes `error.type`, `error.message` and `error.stack_trace`.
- An `extra={"computer_id": …}` lands at `mshkn.computer_id`, not at the top level.
- A value past 4 KiB is truncated and marked.
- The ring buffer evicts oldest-first at `maxlen` and holds nothing else.
- `install_log_buffer` called twice leaves one handler, not two.

**Flow** (real ASGI app, fake host, temp SQLite):

- `GET /logs` with an account key returns `application/x-ndjson`, and every line
  parses as JSON with `ecs.version` present.
- Two accounts: the second sees none of the first's records. This is the
  isolation test.
- A scoped key gets 403.
- `limit` caps the count; `since` drops everything at or before it.

**E2E** — T11.2 becomes a real test. Create a computer, `GET /logs`, and assert
that every line parses, that each carries `@timestamp`, `log.level`, `message`
and `ecs.version`, and that at least one record carries an `mshkn.computer_id`
equal to the computer just created. The last clause is the one that makes it a
test of mshkn rather than of `json.loads` — it proves the log can reconstruct
what happened to a specific computer, which is what the plan's T11.3 asked for.

**Negative control.** Stash `src/`, run T11.2, and confirm it fails on a 404 from
`/logs`. A test that passes without the feature is the failure mode this is
guarding against, and T11.2 has been a placeholder long enough to deserve the
check.

## 10. Note on plan numbering

The e2e file numbers structured JSON logs T11.2. The plan document
(`docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md`) numbers it
T11.3 and gives T11.2 to latency histograms. The whole of phase 11 is offset
this way. This design uses the file's numbering, because that is what the test
class is called. The renumbering is a separate change and is noted in #180.
