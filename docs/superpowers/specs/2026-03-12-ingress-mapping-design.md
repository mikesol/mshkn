# Ingress Mapping Design

**Issue:** #46
**Date:** 2026-03-12
**Status:** Approved

## Problem

External services (Telegram webhooks, CI systems, monitoring alerts) need to trigger disposable computers but can't format requests in mshkn's native API format. Today, each integration needs an external transformer service to massage inputs into the right shape. This doesn't scale — every new integration requires custom glue code.

## Solution

A generic ingress mapping layer that accepts arbitrary HTTP requests and uses user-defined Starlark transformation rules to marshall them into mshkn API calls (fork or create). Rules are stored per-account in the database and managed via CRUD API. Each rule gets a unique capability URL that acts as both the endpoint and the authorization token.

## Architecture

Thin middleware approach: the ingress endpoint runs Starlark in-process, validates the output, then calls existing create/fork code paths internally. No new services, no queues, no request rewriting.

```
External service → POST /ingress/{rule_id}
                       ↓
                   Look up rule in DB
                       ↓
                   Parse request body (JSON, form, raw)
                       ↓
                   Execute Starlark transform(request)
                       ↓
                   Validate return dict
                       ↓
                   Call internal create/fork code path
                       ↓
                   Return result (sync) or 202 (async)
```

## Dependencies

Starlark execution uses the `starlark-go` Python package, which wraps Google's reference Go implementation via CGo. This provides:
- Deterministic execution with guaranteed termination (Starlark language property)
- CPU timeout enforcement at the interpreter level
- Battle-tested sandbox (used by Bazel, Buck2)

If `starlark-go` proves problematic to build, fallback to `pystarlark` with a thread-based timeout wrapper.

## Data Model

### Table: `ingress_rules`

| Column | Type | Description |
|--------|------|-------------|
| internal_id | TEXT PK | Stable UUID, never exposed. Used as FK target for logs. |
| id | TEXT UNIQUE | Rotatable capability token, e.g. `ir_` + 20 random chars. |
| account_id | TEXT FK | Owner account |
| name | TEXT | Human-friendly label (e.g. "telegram-bot") |
| starlark_source | TEXT | Starlark source code containing a `transform` function |
| response_mode | TEXT | `async` (default) or `sync` |
| max_body_bytes | INTEGER | Max request body size, default 10485760 (10MB) |
| rate_limit_rpm | INTEGER | Max requests per minute, default 60 |
| enabled | BOOLEAN | Default true |
| created_at | TEXT | ISO timestamp |
| updated_at | TEXT | ISO timestamp |

Index on `account_id` for listing.

### Table: `ingress_log`

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | UUID per invocation |
| rule_internal_id | TEXT FK | References ingress_rules.internal_id (stable across rotations) |
| status | TEXT | `accepted`, `completed`, `failed`, `rejected` |
| starlark_result | TEXT | JSON of what the transform returned |
| error_message | TEXT | Null on success |
| created_at | TEXT | ISO timestamp |

Pruned after 24h by the existing reaper.

## API: Rule Management (Authenticated)

All endpoints require Bearer token auth via existing middleware.

### `POST /ingress_rules`

Create a new ingress rule.

**Request:**
```json
{
  "name": "telegram-bot",
  "starlark_source": "def transform(req):\n  ...",
  "response_mode": "async",
  "max_body_bytes": 10485760,
  "rate_limit_rpm": 60
}
```

**Validation:** Starlark source must parse and define a `transform` function.

**Response (201):**
```json
{
  "id": "ir_abc123...",
  "name": "telegram-bot",
  "ingress_url": "https://mshkn.dev/ingress/ir_abc123...",
  "response_mode": "async",
  "max_body_bytes": 10485760,
  "rate_limit_rpm": 60,
  "enabled": true,
  "created_at": "2026-03-12T10:00:00Z"
}
```

### `GET /ingress_rules`

List all rules for the authenticated account.

### `GET /ingress_rules/{rule_id}`

Get rule details including `starlark_source`.

### `PUT /ingress_rules/{rule_id}`

Update rule fields. Re-validates Starlark on update.

### `DELETE /ingress_rules/{rule_id}`

Delete rule. Ingress URL stops working immediately.

### `POST /ingress_rules/{rule_id}/rotate`

Regenerate the rule ID. Returns a new `ingress_url`. Old URL stops working immediately.

### `POST /ingress_rules/{rule_id}/test`

Dry-run the Starlark transform against a mock request without executing the action.

**Request:**
```json
{
  "method": "POST",
  "path": "/webhook",
  "headers": {"Content-Type": "application/json"},
  "query_params": {},
  "body": "{\"message\": {\"text\": \"hello\"}}"
}
```

**Response (200):**
```json
{
  "starlark_result": {"action": "fork", "checkpoint_id": "cp_abc", "exec": "echo hello"},
  "validation_errors": [],
  "execution_time_ms": 2
}
```

### `GET /ingress_rules/{rule_id}/logs`

Query recent invocation logs (last 24h).

## API: Ingress Endpoint (Unauthenticated)

### `POST /ingress/{rule_id}`

Also accepts PUT, PATCH, and GET. GET requests pass `None` for all body fields — useful for webhook verification / health checks.

**Flow:**

1. Look up rule by `rule_id` → 404 if not found or disabled
2. Check rate limit → 429 if exceeded
3. Check `Content-Length` against `max_body_bytes` → 413 if exceeded. For chunked transfers without `Content-Length`, read up to `max_body_bytes + 1` bytes and return 413 if the limit is exceeded.
4. Parse body based on `Content-Type`:
   - `application/json` → `body_json` (parsed dict)
   - `multipart/form-data` → `body_form` (parsed fields, files rejected with 413)
   - `application/x-www-form-urlencoded` → `body_form` (parsed dict)
   - anything else → `body_raw` only
5. Build request dict passed to Starlark:
   ```python
   {
     "method": "POST",
     "path": "/ingress/ir_abc...",
     "headers": {"Content-Type": "application/json", ...},
     "query_params": {"key": "value", ...},
     "body_json": {...} or None,
     "body_form": {...} or None,
     "body_raw": "...",
     "content_type": "application/json"
   }
   ```
6. Execute Starlark `transform(request)` with 1s CPU timeout
7. Validate return value:
   - `None` → 204 No Content (rule chose to ignore this request)
   - Dict with `action` field → validate against allowed fields for that action
8. Execute the action using internal code paths (same as create/fork endpoints)
9. Return based on `response_mode`:
   - **async** → `202 {"request_id": "...", "status": "accepted"}` (request_id is for log correlation by the rule owner via `/logs`; async results are delivered via `callback_url` if set)
   - **sync** → `200 {"computer_id": "...", "exec_stdout": "...", "exec_stderr": "...", "exec_exit_code": 0, "created_checkpoint_id": "..."}` (mirrors the full ForkResponse/CreateResponse)

## Starlark Contract

### Input

The `transform` function receives a single dict argument with the parsed request.

### Output

Must return `None` (to ignore) or a dict with an `action` field:

**Fork action:**
```python
{
  "action": "fork",
  "checkpoint_id": "cp_abc123",     # required
  "exec": "echo hello",             # optional
  "self_destruct": True,            # optional
  "exclusive": "defer_on_conflict", # optional: "error_on_conflict" or "defer_on_conflict"
  "callback_url": "https://...",    # optional
}
# Note: label is inherited from the source checkpoint, not set on fork.

```

**Create action:**
```python
{
  "action": "create",
  "capabilities": ["python-3.12"],  # optional
  "exec": "python main.py",         # optional
  "self_destruct": True,            # optional
  "callback_url": "https://..."     # optional
}
```

### Sandbox

- No builtins beyond basic types (dict, list, string, int, bool, None)
- No I/O, no imports, no network access
- 1s CPU timeout (guaranteed termination — Starlark property)
- No access to server state or other rules

### Example Rule

Telegram webhook → fork from a checkpoint with the message text:

```python
def transform(req):
    body = req["body_json"]
    if not body or "message" not in body:
        return None  # ignore non-message updates

    text = body["message"].get("text", "")
    chat_id = str(body["message"]["chat"]["id"])

    return {
        "action": "fork",
        "checkpoint_id": "cp_telegram_agent",
        "exec": "python handle_message.py " + chat_id + " " + repr(text),
        "self_destruct": True,
        "exclusive": "defer_on_conflict",
    }
```

## Security Model

- **Rule management** requires normal Bearer token auth (existing middleware)
- **Ingress URL** is unauthenticated — the `rule_id` is the shared secret (capability URL pattern, same as Stripe webhooks)
- Rule IDs are opaque, randomly generated — cannot be guessed or squatted
- Users can rotate rule IDs via `/rotate` if one leaks
- Per-rule rate limiting prevents abuse (default 60 req/min, in-memory sliding window counters consistent with existing per-key rate limiter; counters reset on server restart)
- Starlark sandbox prevents rule code from accessing server internals
- 10MB body limit prevents memory exhaustion

## Error Handling

| Scenario | HTTP Status | Response |
|----------|-------------|----------|
| Rule not found / disabled | 404 | `{"error": "not_found"}` |
| Rate limit exceeded | 429 | `{"error": "rate_limited"}` |
| Body too large | 413 | `{"error": "payload_too_large"}` |
| Starlark parse error (on create/update) | 400 | `{"error": "invalid_starlark", "detail": "..."}` |
| Starlark runtime error | 502 | `{"error": "transform_error", "detail": "..."}` (sanitized) |
| Starlark timeout | 504 | `{"error": "transform_timeout"}` |
| Invalid return value | 502 | `{"error": "invalid_transform_result", "detail": "..."}` |
| Upstream fork/create error | varies | Proxied error in sync mode; logged in async mode |

## Not In Scope

- File upload handling via ingress (use the upload API directly)
- Telegram-specific integration in the API server (Telegram bridge remains external)
- Cron / scheduled triggers (separate feature)
- HMAC signature verification on incoming requests (can be added later if needed)
- Ingress response body transformation (the response is always mshkn-shaped, not caller-shaped)
