# A model gateway: the liturgy spoken across models, through a hosted Anthropic wire

Date: 2026-09-13. Implements #127; retires the LiteLLM requirement recorded in
`docs/infrastructure.md` under "A model gateway"; depends on the capabilities
driver of `docs/superpowers/specs/2026-09-12-capabilities-design.md`, which is
where `embryo/membrane/capability.py` lives.

## 1. Why

Spec §11 says a capability is spoken "across models where useful", and it never
has been: every run under `docs/embryo/` is `claude-opus-5`. The #123 round cost
about $10.60 for six runs of one model, which is the second reason to want a
second provider — a cheaper backend makes a round of six affordable enough to run
often.

The obstacle was never the membrane. It was that #127 recorded the way through as
"an always-on LiteLLM proxy reachable over HTTPS from the mshkn host, holding the
provider credentials", and filed the issue as blocked on renting one.

## 2. What has changed since #127 was written

Hosted gateways now speak the Anthropic Messages API directly, so there is no
proxy to rent, secure or keep patched. Vercel AI Gateway is the closest fit and
this document is written against it:

- `POST https://ai-gateway.vercel.sh/v1/messages`.
- It accepts both `x-api-key` and `anthropic-version: 2023-06-01`, which is
  exactly what `request_headers` (`embryo/membrane/model.py:115`) already emits.
  The membrane's header path changes not at all.
- `cache_control` is passed through to Anthropic and returned as
  `cache_creation_input_tokens` / `cache_read_input_tokens`, so the one cache
  breakpoint #126 bought survives the move.
- Model ids are namespaced `provider/model`: `anthropic/claude-opus-5`.
- API keys work from any machine; no Vercel project or deployment is involved.
- A key carries a spend budget, set in the dashboard. A brain that loops bills
  per turn, and the budget is the only stop that does not depend on the organism
  behaving.
- BYOK (the operator's own Anthropic key, held by the gateway) carries no
  markup, so the control run of §7 costs what the six existing runs cost and
  stays comparable to them.

OpenRouter was considered and rejected: its Anthropic skin is real, and its model
catalogue is wider, but it authenticates with `Authorization: Bearer` only —
which `request_headers` would have to grow a second form for — and it routes a
model id across providers by default, which is a reproducibility hazard for a
harness whose only output is evidence.

Together AI was considered and rejected as a gateway: it is OpenAI-compatible
only and exposes no `/v1/messages`. It remains available as a *backend behind*
the gateway, which is the role it can actually play.

Self-hosting LiteLLM, as #127 specified, was considered and rejected: it buys
zero markup and full control at the cost of a second always-on host, and it
interposes its own translation layer — which is precisely the "measuring the
gateway rather than the organism" confound #127 warns about, added deliberately
rather than accepted reluctantly.

## 3. What exists today

- `embryo/membrane/model.py` speaks one wire format: `compose_request` (line 76)
  builds the Messages body (`model`, `max_tokens`, `stream`, `system`,
  `messages`, `tools`, `output_config.effort`, `cache_control`);
  `request_headers` (line 115) emits `anthropic-version`, `content-type` and
  `x-api-key`; `parse_message` reads Anthropic content blocks and `usage`.
- `embryo/membrane/turn.py:312` (`post_request`) targets
  `f"{settings.anthropic_base_url}/v1/messages"`.
- `embryo/membrane/config.py:84` reads `ANTHROPIC_BASE_URL` from `/brain/.env`,
  default `https://api.anthropic.com`, trailing slash stripped.
- `embryo/hatch.sh:75` defaults `ANTHROPIC_BASE_URL`, line 92 bakes it into the
  brain key's `relay.targets` as the one permitted prefix, line 115 writes it
  into `/brain/.env`.
- `embryo/membrane/capability.py:73` (`load_run_settings`) reads the operator's
  `.env`: `REQUIRED` (line 46) is `MSHKN_API_URL`, `MSHKN_API_KEY`,
  `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`; `OPTIONAL` (line 47) is `BRAIN_API_URL`.
  `ANTHROPIC_BASE_URL` is in neither.
- `capability.py:664` (`hatch`) builds the subprocess environment from
  `**os.environ` plus eight explicit names. `ANTHROPIC_BASE_URL` is not among
  them, so today it reaches `hatch.sh` only by leaking through `**os.environ`.
- `capability.py:107` is `PRICES = {"claude-opus-5": Price(input=5.0,
  output=25.0)}`; `cost_usd` (line 112) does `PRICES[model_id]` and is called at
  line 1171 while assembling `summary`, after the run has finished spending.
- `embryo/membrane/effort.py:21` is `API_DEFAULT = "high"`; `resolve` (line 53)
  floors the run's default at it and lets `prior_for` and the model's own
  `effort` tool raise it. `turn.py:300` (`_effort_for`) calls it per model call.
- `embryo/membrane/memory.py:120` builds mem0's embedder with the OpenAI key, in
  process. It is a direct SDK call and never touches the relay.

So the model id and the base URL are already parameters. What is not yet true is
that a *namespaced* model id, or a base URL set anywhere but the shell, or a
model without an effort axis, can survive a run.

## 4. The wire: one format still

`ANTHROPIC_BASE_URL=https://ai-gateway.vercel.sh` and
`MEMBRANE_MODEL_ID=anthropic/claude-opus-5`. Reaching a second model costs the
membrane nothing: `request_headers` and `parse_message` are untouched,
`turn.py`'s target is untouched, and the base URL is already forwarded by
`hatch.sh` and already pinned as the key's one relay target.

#127's central bet — one wire format, one relay target, many models, no second
composer or parser in the organism — holds, and this document does not spend it.

The two additions that do reach the membrane, `compose_request`'s opaque body
mapping (§8) and the effort sentinel (§7), are both operator sentences carried
through. Neither inspects a provider, and neither branches on one. That is the
line this document holds: the organism gains knobs, never knowledge.

## 5. Settings: two key slots, not one

`ANTHROPIC_API_KEY` keeps its name and its meaning: the operator's Anthropic key.
A new `AI_GATEWAY_API_KEY` joins it. When `ANTHROPIC_BASE_URL` is not the default,
the gateway key is what rides in `x-api-key`; otherwise the Anthropic key does.

Both live in the operator's `.env` at once, so `capability run --base-url …`
flips a run between direct and gateway without editing secrets — which matters
for §7, where the two are compared.

- `RunSettings` gains `base_url: str` and `gateway_api_key: str | None`.
- `load_run_settings` reads `ANTHROPIC_BASE_URL` and `AI_GATEWAY_API_KEY` by
  adding both to `OPTIONAL`, and raises if the base URL is non-default while the
  gateway key is missing.
- `hatch()` passes `ANTHROPIC_BASE_URL` explicitly rather than relying on the
  `**os.environ` leak, and resolves the two operator slots down to one key.
- `capability run` gains `--base-url`.

The two slots are the operator's side only. `hatch.sh` writes exactly one model
key into `/brain/.env`, still called `ANTHROPIC_API_KEY`, and the brain never
learns whether it is an Anthropic key or a gateway key — it is the key for
whatever `ANTHROPIC_BASE_URL` names. `config.py` and `request_headers` are
therefore unchanged, and a brain checkpoint never carries a credential for a
service it cannot reach.

### 5.1 The second Anthropic caller: mem0's extraction LLM

One key in the brain is only true if nothing else in the brain calls Anthropic.
Something does. `memory.py:124` builds mem0's fact-extraction LLM through
`extraction_llm` (line 83) with `provider: "anthropic"`, a hardcoded
`EXTRACTION_MODEL_ID = "claude-haiku-4-5-20251001"`, and
`settings.anthropic_api_key`. It is an in-process Anthropic SDK client. It never
touches the relay, it never reads `settings.anthropic_base_url`, and it runs on
every turn that recalls or writes memory.

Left alone, handing the brain a gateway key under the name `ANTHROPIC_API_KEY`
breaks every memory operation of every gateway run — the same failure mode as the
`insufficient_quota` stall recorded in `docs/infrastructure.md`, which cost the
first attempt at #101.

Extraction is therefore routed through the gateway too. mem0's `AnthropicLLM`
reads `self.config.anthropic_base_url` before falling back to the environment, so
`extraction_llm` gains a `base_url` argument and `Mem0Store.open` passes
`settings.anthropic_base_url`. `EXTRACTION_MODEL_ID` is namespaced by the same
`bare_model_id` rule in reverse: the gateway needs `anthropic/claude-haiku-…`,
the direct API needs the bare id, so the id is composed from the base URL rather
than stored twice.

One key, one budget, one credential for #92 to move. The alternative — a second
key in `/brain/.env` — was rejected: it doubles what a brain checkpoint carries at
exactly the moment #91 and #92 are trying to take credentials off it.

mem0's `enable_sampling_parameters` model-family sniffing is unaffected by the
prefix: `extraction_llm` sets the flag explicitly to `False`, which short-circuits
the sniff before it parses the name.

`ANTHROPIC_API_KEY` is **not** renamed to something provider-neutral. #91 and #92
are about to move every model key onto account secrets injected at exec; renaming
now means touching `hatch.sh`, `config.py`, `capability.py`, the tests and the
docs twice, for a name that is about to move anyway.

## 6. Cost: normalise the id, and stop raising

Two changes to `cost_usd`:

1. Strip a leading `<provider>/` before the `PRICES` lookup. Then
   `anthropic/claude-opus-5` finds the existing entry, and the six runs already
   in `docs/embryo/` remain comparable to everything measured after this lands.
   Without it, even the control run of §7 fails.
2. Return `None` on an unknown model instead of raising. The run records
   `cost_usd: null` and keeps its evidence.

The second is the point, not collateral. `cost_usd` runs at `capability.py:1171`
while `summary` is being assembled — after every turn has been spoken and paid
for, and before `record.summary(summary)` writes anything. A `KeyError` there
destroys the whole run record of a run that has already cost money.
`tests/unit/test_embryo_capability.py:100` currently asserts that it raises; that
assertion is inverted, deliberately.

`PRICES` gains an entry per model actually measured, keyed by the bare id.

## 7. Effort: the operator asserts, the organism does not sniff

`output_config.effort` is Anthropic-specific. `resolve` floors the run's default
at `API_DEFAULT = "high"`, so `prior_for` or the model's own `effort` tool can put
the field on the wire even when `MEMBRANE_EFFORT` is unset. Against a
non-Anthropic backend that is a 400 waiting for the first irreversible verb.

`MEMBRANE_EFFORT=off` is added as an explicit sentinel. `_effort_for` then returns
`None` unconditionally, no `output_config` is composed, and the run's `summary`
records `effort_supported: false` so `docs/embryo/README.md` can say the axis is
*absent* from a run rather than let a reader assume it was the API's default.

`off` is not a member of `EFFORTS` and must not become one — it is not a rung on
the ladder, it is the absence of the ladder. Three places validate against
`EFFORTS` and each must admit it beside them: `config.py`'s check on
`MEMBRANE_EFFORT`, `capability run --effort`'s `choices`, and `Settings`, which
gains a distinct `effort_enabled: bool` rather than smuggling the sentinel through
`default_effort: str | None` where `None` already means "the API's default".

The alternative — deriving support from the model id's provider prefix — is
rejected. That is provider knowledge living inside the organism, decided by the
organism, which is the exact thing #127 refused when it rejected a second
composer. The operator knows which backend they pointed at; they can say so.

This is the axis that does not survive the crossing, and #127 asked for it to be
recorded rather than discovered. The 2026-09-10 round's headline finding was that
medium effort beat the API default on cost, time and outcome at once. That finding
is Anthropic-only and cross-provider runs cannot be compared on it.

## 8. Provider pinning

The gateway may route `anthropic/claude-opus-5` to Anthropic, Bedrock or Vertex,
and those differ on beta fields and on caching behaviour. For a harness whose only
output is evidence, an unpinned run is not reproducible.

`compose_request` gains an opaque mapping merged into the top level of the body,
never inspected. It reaches the membrane as `MEMBRANE_BODY_EXTRA`, one line of
JSON in `/brain/.env`, parsed once by `config.py` into `Settings.body_extra:
dict` and defaulting to empty. `capability run --body-extra` sets it, `hatch.sh`
writes it through, and `run.json` records it beside the model id. For the control
run of §9 its value is
`{"providerOptions": {"gateway": {"only": ["anthropic"]}}}`.

Opaque and untyped, deliberately: a `gateway_provider` field would be the organism
knowing what a provider is. A dict it copies onto the body is the operator's
sentence, passed through. A malformed value fails at `load_settings`, before a
turn is spoken, not at the relay.

## 9. Sequence: the control run before the second model

Before any second model is measured, run `anthropic/claude-opus-5` through the
gateway and compare it against the six direct runs in `docs/embryo/`.

If the numbers match, the gateway is transparent and any later divergence is the
model. If they do not, that is learned for the price of one run rather than
misattributed to whatever cheap backend is tried first. #127 does not ask for
this and it is the most valuable single run in the sequence.

Only then is a second model hatched, with `MEMBRANE_EFFORT=off`.

## 10. Documentation

- `docs/infrastructure.md`: the "A model gateway" section loses its requirements
  table. There is no host to rent — one API key, a spend budget, and optionally a
  BYOK credential.
- `docs/embryo/README.md`: beside any cross-provider run, three caveats. The two
  #127 asked for — that the effort axis is absent (§7), and that tool-use
  fidelity varies by backend, so a low score on a cheaper model may be measuring
  the gateway's translation rather than the organism — and a third #127 did not
  know about.

The third is the larger of them. **A cross-model run is never purely
cross-model.** `EXTRACTION_MODEL_ID` is fixed at `claude-haiku-4-5-20251001`
(§5.1), so whatever model speaks the liturgy, the facts it remembers were
extracted by Claude Haiku. Memory shapes every turn after the one that wrote it,
so this reaches further into a run than either of the other two caveats, and a
reader comparing two models across the `docs/embryo/` evidence must know that
this variable was held constant rather than crossed.

Making the extraction model a parameter is deliberately out of scope here — it is
a second measurement axis, not a gateway change, and it deserves its own issue.

## 11. Testing

- Unit: `load_run_settings` reads the base URL and the gateway key from `.env`
  and rejects a non-default base URL without one; `cost_usd` normalises a
  `provider/` prefix and returns `None` for an unknown id (inverting
  `test_embryo_capability.py:102`); `_effort_for` returns `None` under
  `MEMBRANE_EFFORT=off` even when `prior_for` argues for `high`;
  `compose_request` merges `body_extra` onto the body and an unparseable
  `MEMBRANE_BODY_EXTRA` fails `load_settings` rather than a turn.
- Flow: `tests/flow/test_capabilities.py:148` already writes
  `ANTHROPIC_BASE_URL=http://model` into the fake brain's `.env`. Extend it for a
  namespaced model id, a run with effort off, and a run whose `body_extra`
  reaches the fake endpoint intact.
- Live: the control run of §9. It is the only tier that can prove the gateway
  accepts what the membrane sends.

## 12. The open question, to be settled first

Whether the gateway accepts or rejects `output_config.effort` for `anthropic/*`
models cannot be settled from its documentation — the parameter list does not
name the field. It decides whether the effort axis survives the move for
Anthropic models at all, and therefore whether the control run of §9 is a like-for-like
comparison or already a different measurement.

The first task of the plan is a one-call probe against
`https://ai-gateway.vercel.sh/v1/messages` with `output_config.effort` set. It
costs about a cent and it gates §7's shape: if the field is accepted, `off` is
only for non-Anthropic runs; if it is rejected, the control run must itself be
run with effort off, and the six existing runs are not directly comparable to
anything through the gateway.

### 12.1 Probed 2026-09-15: settled. The effort field survives.

The 403 of 2026-09-13 was billing verification, and clearing it exposed a second
gate — a free tier that serves `zai/*` and restricts everything else with 403
`RestrictedModelsError`. Both are now behind us; the answers below were taken on
paid credits.

**The question this section was opened for: `output_config.effort` reaches
Anthropic models through the gateway intact.** `{"effort": "medium"}` on
`anthropic/claude-opus-5` returns 200. The negative control is what makes that an
answer rather than a shrug: `{"effort": "banana"}` returns 400, `output_config.effort:
Invalid option: expected one of "low"|"medium"|"high"|"max"|"xhigh"`, so the
field is parsed and validated rather than tolerated and dropped. The same prompt
at the API default returned a `thinking` block where the `medium` call returned
plain text, which is the field being acted on and not merely accepted.

So §7's `off` sentinel is needed **only for non-Anthropic runs**, and the control
run of §9 is a like-for-like comparison against the six direct runs — the better
of the two outcomes Task 1 of the plan laid out.

**And the skin is real for a non-Anthropic model**, which is the load-bearing bet
of §4 and had never been tested. `zai/glm-4.7` returns an Anthropic `message`
with a `content` array and `usage`; streamed with one tool it emits
`message_start`, `content_block_start` / `_delta` / `_stop`, `message_delta` and
`message_stop`, and the opening block is `{"type": "tool_use", …}` — exactly what
`parse_message` reads at `model.py:139`.

One caveat on that model. The gateway validates `output_config.effort` for
`zai/glm-4.7` by the same schema and accepts `medium` with a 200. That is the
*skin* validating, not zai honouring: nothing in the response says the value
reached a model with such an axis. A GLM run is still to be spoken with
`MEMBRANE_EFFORT=off` — the axis there is unmeasurable, which is what §7 means by
absent, and a 200 is not evidence against it.

### 12.2 The extraction slug §5.1 composes does not exist

`GET https://ai-gateway.vercel.sh/v1/models` on 2026-09-15 returned 372 models.
The Anthropic Haiku entries are `anthropic/claude-3-haiku` and
`anthropic/claude-haiku-4.5`. There is no dated slug in the catalogue at all, so
`f"anthropic/{EXTRACTION_MODEL_ID}"` — the composition §5.1 chose precisely to
avoid storing the id twice — names nothing, and would have failed every memory
operation of every gateway run: the exact failure §5.1 was written to prevent.

The reasoning was sound and the premise was wrong. A gateway is a second naming
authority, not a prefix on the first. `EXTRACTION_GATEWAY_MODEL_ID` is therefore
stored beside `EXTRACTION_MODEL_ID` rather than derived from it, and §5.1's "the
id is composed from the base URL rather than stored twice" no longer holds.

`anthropic/claude-haiku-4.5` answers 200 on paid credits, so the corrected id is
live and not merely catalogued. Nothing caught the old one and nothing could
have: the live probe that would have was Task 1, and Task 1 was blocked on
billing. `bare_model_id` in the other direction (§6) is unaffected — stripping a
prefix that is present is not the same bet as inventing one that must be.

## 13. Rejected

- **A second request composer and parser in `model.py`**, per #127. Provider
  handling inside the organism for a reason that has nothing to do with the
  organism.
- **Self-hosted LiteLLM**, per §2.
- **OpenRouter**, per §2.
- **Renaming `ANTHROPIC_API_KEY`**, per §5.
- **Deriving effort support from the model id**, per §7.
- **Reading cost from the gateway's own accounting** rather than a local price
  table. It would be more accurate and it makes the run record depend on a second
  API call to a third party, whose shape is not the Messages API and would have to
  be parsed. The price table is already the repo's answer and is wrong only when
  prices move.
- **Routing the mem0 *embedder* through the gateway.** Unlike the extraction LLM
  of §5.1, it is not a Messages API caller: it is `text-embedding-3-small` over
  the OpenAI SDK (`memory.py:120`), the gateway's Anthropic skin has nothing to
  say about it, and the OpenAI key it uses is unaffected by any of this. The
  embedder stays direct and the `OPENAI_API_KEY` requirement is unchanged.
- **A second key in `/brain/.env`** to keep mem0's extraction on the direct
  Anthropic API, per §5.1.
- **Making `EXTRACTION_MODEL_ID` a parameter**, per §10. A second measurement
  axis wearing a gateway change's clothes.

## 14. Facts checked before the plan

- `request_headers` (`model.py:115`) emits `anthropic-version`, `content-type`
  and `x-api-key`; the gateway's documented curl for `/v1/messages` sends
  `anthropic-version: 2023-06-01`, and its authentication page names `x-api-key`
  as an accepted form. No header change is required.
- `hatch.sh:75`, `:92` and `:115` already default, pin and persist
  `ANTHROPIC_BASE_URL`; `config.py:84` already reads it.
- `capability.py:46-47`: `ANTHROPIC_BASE_URL` is in neither `REQUIRED` nor
  `OPTIONAL`. `capability.py:664`: `hatch()` does not pass it explicitly.
- `capability.py:107` is a one-entry `PRICES`; `cost_usd` at line 111 subscripts
  it directly; line 1146 calls it after the run has spent.
  `tests/unit/test_embryo_capability.py:99-102` pins both the arithmetic and the
  `KeyError`.
- `effort.py:21` `API_DEFAULT = "high"`; `resolve` (line 53) returns `None` only
  when the default is unset *and* neither the prior nor the request names an
  effort. `turn.py:300` calls it per model call and appends to `pending.efforts`.
- `memory.py:120` builds the embedder with `provider="openai"` and the operator's
  OpenAI key; nothing about the model gateway touches it.
- `memory.py:124` builds mem0's extraction LLM with `extraction_llm` (line 83):
  `provider="anthropic"`, `EXTRACTION_MODEL_ID = "claude-haiku-4-5-20251001"`
  (line 32), `settings.anthropic_api_key`, and `enable_sampling_parameters:
  False`. It is an in-process SDK client and reads neither the relay nor
  `settings.anthropic_base_url`. This is the fact that produced §5.1 and it was
  not known when §5 was first written.
- mem0's `AnthropicLLM.__init__` resolves `base_url` as
  `self.config.anthropic_base_url or os.getenv("ANTHROPIC_BASE_URL")` and passes
  it to `anthropic.Anthropic(**client_kwargs)` only when truthy. The brain's
  `.env` is parsed by `parse_env`, never exported to `os.environ`, so today that
  fallback finds nothing and extraction goes direct. Routing it is a config
  field, not a patch.
- `AnthropicLLM._enable_sampling_parameters` returns the explicit flag before it
  parses the model name, so a `provider/` prefix on `EXTRACTION_MODEL_ID` cannot
  reach the family sniff.
- The gateway returned `403 customer_verification_required` to a probe with
  `x-api-key` on 2026-09-13, not `401`: the key and the header form are accepted,
  and the team needs a card on file before anything else can be measured.
- `tests/flow/test_capabilities.py:148` writes `ANTHROPIC_BASE_URL=http://model`,
  so the flow tier already has a fake model endpoint to extend.
- `embryo/membrane/capability.py` does not exist on `origin/main`; it arrives with
  the capabilities branch, which this work therefore builds on.
