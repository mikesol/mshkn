# Model Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a capability be spoken to a model that is not Anthropic's, by pointing `ANTHROPIC_BASE_URL` at Vercel AI Gateway, without a second request composer or response parser in the membrane.

**Architecture:** The wire does not change — the gateway speaks the Anthropic Messages API and accepts the headers `request_headers` already emits. What changes is everything around the wire that assumed one model: a price table that raises on an unknown id, a base URL that never reaches `hatch.sh`, an Anthropic-only `output_config.effort` that can reach a non-Anthropic backend, and a second in-process Anthropic caller in mem0 that would break the moment the one key slot holds a gateway key.

**Tech Stack:** Python 3.12, `uv`, pytest, httpx, mem0, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-09-13-model-gateway-design.md`

## Global Constraints

- Every tool runs through the project venv as `uv run <tool>`. Never call `python`, `pytest` or `ruff` directly.
- Green means: `uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov`. Zero warnings is part of green.
- Coverage floor is 98 % (`fail_under` in `pyproject.toml`). `pytest --cov` runs the unit and flow tiers; the E2E tier is deselected by default.
- `tests/unit/test_docs.py` fails when a document in its `DOCS` tuple names a path, module, route, metric or variable that does not exist. `docs/infrastructure.md` and `docs/embryo/README.md` are both in `DOCS`. **A documentation edit must therefore land in the same commit as the code that creates the names it mentions** — never before.
- The default base URL is the string `https://api.anthropic.com`, already `DEFAULT_ANTHROPIC_BASE_URL` in `embryo/membrane/config.py:15`. Do not introduce a second spelling of it.
- The gateway namespaces every model id as `provider/model`: `anthropic/claude-opus-5`, `anthropic/claude-haiku-4-5-20251001`.
- Branch: `model-gateway-127`, off `capabilities-design`. `embryo/membrane/capability.py` does not exist on `origin/main`.
- Never print the value of `AI_GATEWAY_API_KEY`, `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` into a log, a test fixture or a commit.

---

### Task 1: Probe the gateway and settle the effort question

Spec §12. This task produces an **answer**, not code. It gates Task 5's default and Task 8's comparability. Everything else in the plan can proceed without it.

**Blocked on:** a payment method on the Vercel team. A probe run before that returns `403 customer_verification_required`, which is not an answer to the question being asked.

**Files:**
- Modify: `docs/superpowers/specs/2026-09-13-model-gateway-design.md` (§12 records the finding)

- [ ] **Step 1: Confirm billing is live with the cheapest possible call**

```bash
cd /home/mikesol/Documents/GitHub/mshkn
KEY=$(grep '^AI_GATEWAY_API_KEY=' .env | cut -d= -f2- | tr -d '"'"'"' \r')
curl -s -o /tmp/probe-a.json -w "status %{http_code}\n" -X POST https://ai-gateway.vercel.sh/v1/messages \
  -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
  -d '{"model":"anthropic/claude-opus-5","max_tokens":16,"messages":[{"role":"user","content":"say hi"}]}'
cat /tmp/probe-a.json
```

Expected: `status 200` and a body with a `content` array. A `403 customer_verification_required` means billing is still not enabled — stop, and report it. Do not proceed to Step 2 on a 403; the later probes will all fail the same way and tell you nothing.

- [ ] **Step 2: Probe `output_config.effort`, the question the task exists for**

```bash
KEY=$(grep '^AI_GATEWAY_API_KEY=' .env | cut -d= -f2- | tr -d '"'"'"' \r')
curl -s -o /tmp/probe-b.json -w "status %{http_code}\n" -X POST https://ai-gateway.vercel.sh/v1/messages \
  -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
  -d '{"model":"anthropic/claude-opus-5","max_tokens":16,"stream":false,
       "output_config":{"effort":"medium"},
       "messages":[{"role":"user","content":"say hi"}]}'
cat /tmp/probe-b.json
```

Two possible answers, both useful:
- **`200`** — the field survives. Task 5's `off` sentinel is only ever needed for non-Anthropic runs, and Task 8's control run is a like-for-like comparison against the six existing runs.
- **`400`** — the field does not survive. The control run must itself be run with `MEMBRANE_EFFORT=off`, and no run through the gateway is directly comparable to the six existing ones on any axis that effort touches. Say so loudly; it is the more consequential outcome.

- [ ] **Step 3: Probe streaming with a tool, since the membrane always streams**

`compose_request` sets `"stream": True` unconditionally (`model.py:88`) and the liturgy is nothing but tool calls, so a gateway that handles neither is no use whatever it does with effort.

```bash
KEY=$(grep '^AI_GATEWAY_API_KEY=' .env | cut -d= -f2- | tr -d '"'"'"' \r')
curl -s -N -X POST https://ai-gateway.vercel.sh/v1/messages \
  -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
  -d '{"model":"anthropic/claude-opus-5","max_tokens":256,"stream":true,
       "tools":[{"name":"remember","description":"Store a fact.",
                 "input_schema":{"type":"object","properties":{"fact":{"type":"string"}},"required":["fact"]}}],
       "messages":[{"role":"user","content":"Use the remember tool to store: the sky is blue."}]}' \
  | grep -E '^event:' | sort -u
```

Expected: the event names include `content_block_start`, `content_block_delta` and `message_delta`. Confirm from the raw stream that a `tool_use` block appears — that is what `parse_message` reads at `model.py:139`.

- [ ] **Step 4: Probe `cache_control` and the usage fields the cost table needs**

```bash
KEY=$(grep '^AI_GATEWAY_API_KEY=' .env | cut -d= -f2- | tr -d '"'"'"' \r')
python3 - <<'PY' > /tmp/probe-d.json
import json, os, subprocess
key = next(l.split("=",1)[1].strip().strip("'\"")
           for l in open(".env") if l.startswith("AI_GATEWAY_API_KEY="))
body = {
    "model": "anthropic/claude-opus-5", "max_tokens": 16,
    # Caching needs ~1024 tokens before a provider will store anything.
    "system": [{"type": "text", "text": "You are terse. " * 400,
                "cache_control": {"type": "ephemeral"}}],
    "messages": [{"role": "user", "content": "hi"}],
}
print(subprocess.run(["curl","-s","-X","POST","https://ai-gateway.vercel.sh/v1/messages",
    "-H",f"x-api-key: {key}","-H","anthropic-version: 2023-06-01",
    "-H","content-type: application/json","-d",json.dumps(body)],
    capture_output=True, text=True).stdout)
PY
cat /tmp/probe-d.json
```

Expected: a `usage` object carrying `cache_creation_input_tokens`. Run it twice; the second run should show `cache_read_input_tokens` instead. If neither field ever appears, #126's cache breakpoint is dead through the gateway and every cost figure after this is wrong — record that in §12 as a finding of its own.

- [ ] **Step 5: Probe the provider pin of Task 6**

```bash
KEY=$(grep '^AI_GATEWAY_API_KEY=' .env | cut -d= -f2- | tr -d '"'"'"' \r')
curl -s -o /tmp/probe-e.json -w "status %{http_code}\n" -X POST https://ai-gateway.vercel.sh/v1/messages \
  -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
  -d '{"model":"anthropic/claude-opus-5","max_tokens":16,
       "providerOptions":{"gateway":{"only":["anthropic"]}},
       "messages":[{"role":"user","content":"say hi"}]}'
cat /tmp/probe-e.json
```

Expected: `200`. A `400` means the pin's shape is wrong and Task 6's recorded value must change; the mechanism (an opaque passthrough) does not.

- [ ] **Step 6: Record the findings in the spec**

Replace §12's "to be settled first" with what was observed: the status code for each probe, whether `output_config.effort` survives, whether the cache fields appear, and the exact `providerOptions` value that returned 200. Date the finding `2026-09-13` or later. Keep it short — five lines of fact, not a narrative.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-09-13-model-gateway-design.md
git commit -m "The gateway probe: what survives the crossing, measured not assumed"
```

---

### Task 2: A price table that survives a namespaced id

Spec §6. Independent of every other task; do it first among the code tasks because it is the smallest and it unblocks reading any gateway run's evidence.

**Files:**
- Modify: `embryo/membrane/capability.py:107-121`
- Modify: `embryo/membrane/capability.py:1171` and the log line at `:1153-1157`
- Test: `tests/unit/test_embryo_capability.py:91-102`

**Interfaces:**
- Produces: `membrane.capability.bare_model_id(model_id: str) -> str`; `membrane.capability.cost_usd(usage: Mapping[str, int], model_id: str) -> float | None` (was `-> float`, raised `KeyError`).

- [ ] **Step 1: Write the failing tests**

Replace the body of `test_cost_uses_the_price_table_and_the_cache_multipliers` in `tests/unit/test_embryo_capability.py` (line 91) and add two tests after it:

```python
def test_cost_uses_the_price_table_and_the_cache_multipliers() -> None:
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 100_000,
        "cache_creation_input_tokens": 200_000,
        "cache_read_input_tokens": 1_000_000,
    }
    # 5.00 + 2.50 + 0.25 * 5 + 0.1 * 5 = 5 + 2.5 + 1.25 + 0.5
    assert cost_usd(usage, "claude-opus-5") == pytest.approx(9.25)
    assert cost_usd(zero_usage(), "claude-opus-5") == 0.0


def test_a_gateway_id_prices_as_the_model_it_names() -> None:
    """A hosted gateway namespaces every id by its provider. The six runs spoken
    before the gateway existed must stay comparable to the ones spoken through it,
    so the prefix is stripped rather than given a second price row."""
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    assert cost_usd(usage, "anthropic/claude-opus-5") == cost_usd(usage, "claude-opus-5")
    assert bare_model_id("anthropic/claude-opus-5") == "claude-opus-5"
    assert bare_model_id("claude-opus-5") == "claude-opus-5"


def test_an_unpriced_model_costs_nothing_known_rather_than_losing_the_run() -> None:
    """`cost_usd` is called while the summary is assembled, after every turn has
    been spoken and paid for. A raise there throws away the evidence of a run that
    has already cost money, which is the worst moment this code could choose."""
    assert cost_usd(zero_usage(), "moonshot/kimi-k2") is None
    assert cost_usd({"input_tokens": 10}, "claude-unknown") is None
```

Add `bare_model_id` to the `from membrane.capability import (...)` block at the top of the file (line 19, alphabetically before `cost_usd`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_embryo_capability.py -k "cost or gateway_id or unpriced" -v`
Expected: FAIL — `ImportError: cannot import name 'bare_model_id'`.

- [ ] **Step 3: Write the implementation**

In `embryo/membrane/capability.py`, replace `cost_usd` (line 112) and add `bare_model_id` above it:

```python
def bare_model_id(model_id: str) -> str:
    """`anthropic/claude-opus-5` as `claude-opus-5`: a gateway namespaces every id by
    its provider, and the price of a model does not change because of the road taken
    to reach it."""
    return model_id.rsplit("/", 1)[-1]


def cost_usd(usage: Mapping[str, int], model_id: str) -> float | None:
    """USD for one run's usage, or None where the model has no price on file.

    None and not a `KeyError`: this is called at the end of `run_once`, while the
    summary is being assembled, after every turn has been spoken and paid for and
    before anything has been written to disk. A raise there loses the whole record
    of a run that has already cost money."""
    price = PRICES.get(bare_model_id(model_id))
    if price is None:
        return None
    return (
        usage.get("input_tokens", 0) * price.input
        + usage.get("cache_creation_input_tokens", 0) * price.input * CACHE_WRITE
        + usage.get("cache_read_input_tokens", 0) * price.input * CACHE_READ
        + usage.get("output_tokens", 0) * price.output
    ) / 1_000_000
```

- [ ] **Step 4: Teach the two call sites that `None` is a price**

At `embryo/membrane/capability.py:1171`, replace the `"cost_usd"` entry. Insert above the `summary = {` literal:

```python
            cost = cost_usd(usage, model_id)
```

and change the entry to:

```python
                "cost_usd": None if cost is None else round(cost, 4),
```

Then the log line below the literal (currently `f"{model_calls} model calls, {tokens}, ${summary['cost_usd']}\n"`) becomes:

```python
            priced = "unpriced" if cost is None else f"${round(cost, 4)}"
            log.write(
                f"{out_dir.name}: {passed}/{len(capability.postconditions)} postconditions, "
                f"{model_calls} model calls, {tokens}, {priced}\n"
            )
```

`$None` in an operator's terminal is a bug report waiting to be filed; `unpriced` is the fact.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_embryo_capability.py -v`
Expected: PASS, all of them. If `test_run_once_hatches_speaks_judges_records_and_tears_down` (line 1913) fails on `summary["cost_usd"] > 0`, the stub model id it uses is unpriced — that is a real signal, not a flake: give the stub `claude-opus-5`.

- [ ] **Step 6: Commit**

```bash
git add embryo/membrane/capability.py tests/unit/test_embryo_capability.py
git commit -m "A namespaced model id prices as the model it names, and an unpriced one keeps its run"
```

---

### Task 3: The base URL and the gateway key reach `hatch.sh`

Spec §5. Does not depend on Task 2.

**Files:**
- Modify: `embryo/membrane/capability.py:46-47` (`REQUIRED`, `OPTIONAL`), `:62-96` (`RunSettings`, `load_run_settings`), `:664-682` (`hatch`), `:1209` (the CLI)
- Modify: `docs/infrastructure.md` (the "A model gateway" section)
- Test: `tests/unit/test_embryo_capability.py:59-89`, and `_settings`/`_stub_hatch` helpers

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `membrane.capability.DEFAULT_BASE_URL: str`; `RunSettings.base_url: str`, `RunSettings.gateway_api_key: str | None`, `RunSettings.model_api_key` (a read-only property returning `str`); `load_run_settings(env_file, environ, *, model_id=None, effort=None, base_url=None)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_embryo_capability.py`, after `test_a_missing_key_is_named` (line 82):

```python
def test_the_base_url_defaults_to_anthropic_and_the_anthropic_key_travels(
    tmp_path: Path,
) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "MSHKN_API_URL=u\nMSHKN_API_KEY=k\nANTHROPIC_API_KEY=sk-a\nOPENAI_API_KEY=oa\n"
    )
    settings = load_run_settings(env, {})
    assert settings.base_url == "https://api.anthropic.com"
    assert settings.gateway_api_key is None
    assert settings.model_api_key == "sk-a"


def test_a_gateway_base_url_sends_the_gateway_key_instead(tmp_path: Path) -> None:
    """The operator holds both slots so `--base-url` alone flips a run; the brain is
    handed one key and never learns which kind it is."""
    env = tmp_path / ".env"
    env.write_text(
        "MSHKN_API_URL=u\nMSHKN_API_KEY=k\nANTHROPIC_API_KEY=sk-a\nOPENAI_API_KEY=oa\n"
        "AI_GATEWAY_API_KEY=vck-1\n"
    )
    settings = load_run_settings(env, {}, base_url="https://ai-gateway.vercel.sh/")
    # The trailing slash goes, as it does in the brain's own config (config.py:84).
    assert settings.base_url == "https://ai-gateway.vercel.sh"
    assert settings.model_api_key == "vck-1"
    assert settings.anthropic_api_key == "sk-a"


def test_a_gateway_base_url_without_a_gateway_key_is_refused_before_it_hatches(
    tmp_path: Path,
) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "MSHKN_API_URL=u\nMSHKN_API_KEY=k\nANTHROPIC_API_KEY=sk-a\nOPENAI_API_KEY=oa\n"
    )
    with pytest.raises(ValueError, match="AI_GATEWAY_API_KEY"):
        load_run_settings(env, {}, base_url="https://ai-gateway.vercel.sh")


def test_the_base_url_comes_from_the_env_file_too(tmp_path: Path) -> None:
    """Today it reaches hatch.sh only by leaking through `**os.environ`."""
    env = tmp_path / ".env"
    env.write_text(
        "MSHKN_API_URL=u\nMSHKN_API_KEY=k\nANTHROPIC_API_KEY=sk-a\nOPENAI_API_KEY=oa\n"
        "AI_GATEWAY_API_KEY=vck-1\nANTHROPIC_BASE_URL=https://ai-gateway.vercel.sh\n"
    )
    assert load_run_settings(env, {}).model_api_key == "vck-1"
```

Then update the existing `test_settings_come_from_the_env_file_and_the_environment_wins` (line 57): its `RunSettings(...)` equality assertion gains nothing, because both new fields default. Leave it as it is — if it passes unchanged, that is the proof the defaults are right.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_embryo_capability.py -k "base_url or gateway_key" -v`
Expected: FAIL — `TypeError: load_run_settings() got an unexpected keyword argument 'base_url'`.

- [ ] **Step 3: Write the implementation**

In `embryo/membrane/capability.py`, extend the two tuples at lines 46-47 and add the default beside them:

```python
REQUIRED = ("MSHKN_API_URL", "MSHKN_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
OPTIONAL = ("BRAIN_API_URL", "ANTHROPIC_BASE_URL", "AI_GATEWAY_API_KEY")
DEFAULT_BASE_URL = DEFAULT_ANTHROPIC_BASE_URL
```

`DEFAULT_ANTHROPIC_BASE_URL` is already imported from `membrane.config` by this module's neighbours; add it to the existing `from membrane.config import ...` line rather than retyping the URL. One spelling of that string in the codebase, not two.

Add the two fields and the property to `RunSettings` (after `default_effort`, line 70):

```python
    # Where the relay forwards a model call, and the key for whatever it names. The
    # operator holds both slots so `--base-url` alone flips a run between the direct
    # API and a gateway; only one of them is ever written into the brain's .env.
    base_url: str = DEFAULT_BASE_URL
    gateway_api_key: str | None = None

    @property
    def model_api_key(self) -> str:
        """The one key the brain is handed. `load_run_settings` refuses a non-default
        base URL without a gateway key, so the fallback below is unreachable and is
        here only so the type is `str` and not `str | None`."""
        if self.base_url == DEFAULT_BASE_URL:
            return self.anthropic_api_key
        return self.gateway_api_key or self.anthropic_api_key
```

In `load_run_settings`, add the parameter and the resolution before the `return`:

```python
def load_run_settings(
    env_file: Path,
    environ: Mapping[str, str],
    *,
    model_id: str | None = None,
    effort: str | None = None,
    base_url: str | None = None,
) -> RunSettings:
```

and, after the `missing` check:

```python
    url = (base_url or values.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    gateway_key = values.get("AI_GATEWAY_API_KEY") or None
    if url != DEFAULT_BASE_URL and not gateway_key:
        raise ValueError(
            f"AI_GATEWAY_API_KEY is required when the model base URL is {url}: "
            f"put it in {env_file} or the environment"
        )
```

with `base_url=url, gateway_api_key=gateway_key` added to the returned `RunSettings`.

- [ ] **Step 4: Run the settings tests**

Run: `uv run pytest tests/unit/test_embryo_capability.py -k "settings or base_url or gateway_key or missing_key" -v`
Expected: PASS.

- [ ] **Step 5: Write the failing test for the hatch environment**

In `tests/unit/test_embryo_capability.py`, extend `_stub_hatch`'s grep (line ~1854) to capture the new variable:

```python
            "env | grep -E '^(MSHKN_API_URL|MSHKN_API_KEY|BRAIN_API_URL|MEMBRANE_MODEL"
            "|MEMBRANE_MODEL_ID|MEMBRANE_EFFORT|ANTHROPIC_API_KEY|ANTHROPIC_BASE_URL"
            "|OPENAI_API_KEY)='"
```

and update `test_hatch_runs_the_script_with_the_keys_and_the_real_model` (line 1892) so the expected text carries the base URL — note the sort order, `ANTHROPIC_BASE_URL` follows `ANTHROPIC_API_KEY`:

```python
    assert out.read_text() == (
        "ANTHROPIC_API_KEY=sk-a\nANTHROPIC_BASE_URL=https://api.anthropic.com\n"
        "BRAIN_API_URL=https://api.mshkn.dev\nMEMBRANE_EFFORT=\n"
        "MEMBRANE_MODEL=anthropic\nMEMBRANE_MODEL_ID=claude-opus-5\nMSHKN_API_KEY=k\n"
        "MSHKN_API_URL=http://api\nOPENAI_API_KEY=oa\n"
    )
```

Add a second test below it:

```python
def test_hatch_hands_the_brain_the_gateway_key_under_the_one_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One model key in /brain/.env, whatever kind it is: a brain checkpoint must not
    carry a credential for a service it cannot reach (#92)."""
    out = tmp_path / "env.txt"
    monkeypatch.setenv("HATCH_ENV_OUT", str(out))
    settings = replace(
        _settings(), base_url="https://ai-gateway.vercel.sh", gateway_api_key="vck-1"
    )
    hatch(settings, _stub_hatch(tmp_path), log=io.StringIO())
    written = out.read_text()
    assert "ANTHROPIC_API_KEY=vck-1\n" in written
    assert "ANTHROPIC_BASE_URL=https://ai-gateway.vercel.sh\n" in written
    assert "sk-a" not in written
```

Add `from dataclasses import replace` to the imports if it is not already there.

- [ ] **Step 6: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_embryo_capability.py -k hatch_ -v`
Expected: FAIL — the written env has no `ANTHROPIC_BASE_URL` line, and `ANTHROPIC_API_KEY=sk-a` where `vck-1` was expected.

- [ ] **Step 7: Pass the base URL and the resolved key from `hatch`**

In `embryo/membrane/capability.py:664`, change two entries of the `env` dict:

```python
        "ANTHROPIC_API_KEY": settings.model_api_key,
        "ANTHROPIC_BASE_URL": settings.base_url,
```

Explicit, not inherited: `**os.environ` above it happens to carry `ANTHROPIC_BASE_URL` when the operator exported it, and a run's model endpoint should not depend on whether a shell was configured.

- [ ] **Step 8: Run the tests**

Run: `uv run pytest tests/unit/test_embryo_capability.py -v`
Expected: PASS.

- [ ] **Step 9: Add `--base-url` to the CLI**

In `main` (`embryo/membrane/capability.py:1209`), after `--model`:

```python
    run.add_argument(
        "--base-url",
        default=None,
        help=f"where the relay forwards a model call (default {DEFAULT_BASE_URL})",
    )
```

In `_run`, extend the existing refusal at line 1295 so a dependent capability rejects `--base-url` alongside `--model` and `--effort` — it starts from a promotion whose brain has the URL baked into its `/brain/.env`:

```python
    if capability.depends and (args.model or args.effort or args.base_url):
        log.write(
            f"{capability.name} starts from {capability.depends[-1]}'s promotion, whose brain "
            "runs the model, effort and base URL baked into its /brain/.env at hatch: "
            "--model, --effort and --base-url belong to a capability that hatches\n"
        )
        return 2
```

and pass it through:

```python
        settings = load_run_settings(
            args.env, os.environ, model_id=args.model, effort=args.effort,
            base_url=args.base_url,
        )
```

Update `test_main_refuses_a_model_for_a_capability_that_does_not_hatch` (line 2570) to also assert the `--base-url` case.

- [ ] **Step 10: Record the base URL in the run's evidence**

In the `summary` literal (`capability.py:~1142`), after `"model": model_id,`:

```python
                "base_url": settings.base_url,
```

A run whose evidence does not say which endpoint spoke it cannot be reproduced.

- [ ] **Step 11: Rewrite the gateway section of `docs/infrastructure.md`**

Replace the whole "A model gateway, for measuring across models" section — the prose and its four-row requirements table — with the following. The old table demands a host that no longer needs renting.

```markdown
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

`ANTHROPIC_BASE_URL` selects it, per run with `capability run --base-url` or once in the operator's
`.env`. `hatch.sh` bakes it into the brain key's `relay.targets`, so it must not move between hatch
and run.

`OPENAI_API_KEY` is unaffected: mem0's embedder is an OpenAI SDK call that never touches the relay
or the gateway.
```

- [ ] **Step 12: Run the doc test and the whole suite**

Run: `uv run pytest tests/unit/test_docs.py -v && uv run pytest --cov`
Expected: PASS. `test_docs.py` checks that every `MSHKN_*`-shaped variable a listed document names exists in the codebase; `AI_GATEWAY_API_KEY` now does, because Step 3 added it to `OPTIONAL`. This is why the doc edit lives in this task and not in a documentation task of its own.

- [ ] **Step 13: Commit**

```bash
git add embryo/membrane/capability.py tests/unit/test_embryo_capability.py docs/infrastructure.md
git commit -m "The model base URL and its key are a run's choice, and reach hatch.sh explicitly"
```

---

### Task 4: mem0's extraction LLM goes through the gateway too

Spec §5.1. **Do not skip this task or reorder it after Task 8.** Without it, Task 3 has handed the brain a gateway key under the name `ANTHROPIC_API_KEY`, and mem0's in-process Anthropic client will send that key to `api.anthropic.com` and fail on every turn that touches memory.

**Files:**
- Modify: `embryo/membrane/memory.py:32`, `:83-97`, `:124`
- Test: `tests/unit/test_embryo_memory.py`

**Interfaces:**
- Consumes: `Settings.anthropic_base_url` (already exists, `config.py:37`).
- Produces: `membrane.memory.extraction_model_id(base_url: str) -> str`; `extraction_llm(api_key: str | None, base_url: str) -> dict[str, Any]` (was `extraction_llm(api_key)`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_embryo_memory.py`:

```python
def test_extraction_goes_direct_when_the_base_url_is_anthropic() -> None:
    llm = extraction_llm("sk-a", DEFAULT_ANTHROPIC_BASE_URL)
    assert llm["config"]["model"] == "claude-haiku-4-5-20251001"
    assert llm["config"]["anthropic_base_url"] == DEFAULT_ANTHROPIC_BASE_URL
    assert llm["config"]["api_key"] == "sk-a"


def test_extraction_follows_the_brain_through_a_gateway() -> None:
    """mem0's extraction LLM is a second Anthropic caller, in process and off the
    relay (§5.1). The brain holds one model key, so if the relay goes through a
    gateway then extraction must too or every memory operation fails."""
    llm = extraction_llm("vck-1", "https://ai-gateway.vercel.sh")
    assert llm["config"]["anthropic_base_url"] == "https://ai-gateway.vercel.sh"
    # The gateway namespaces every id by its provider, including this one.
    assert llm["config"]["model"] == "anthropic/claude-haiku-4-5-20251001"


def test_the_extraction_model_is_namespaced_only_for_a_gateway() -> None:
    assert extraction_model_id(DEFAULT_ANTHROPIC_BASE_URL) == EXTRACTION_MODEL_ID
    assert extraction_model_id("https://ai-gateway.vercel.sh") == f"anthropic/{EXTRACTION_MODEL_ID}"


def test_sampling_parameters_stay_suppressed_through_the_gateway() -> None:
    """mem0 sniffs the model family out of the id to decide whether to send
    `temperature`, and a `provider/` prefix would change what it parses. The explicit
    flag short-circuits the sniff, so the prefix cannot reach it — pin that."""
    for url in (DEFAULT_ANTHROPIC_BASE_URL, "https://ai-gateway.vercel.sh"):
        assert extraction_llm("k", url)["config"]["enable_sampling_parameters"] is False
```

Add `EXTRACTION_MODEL_ID`, `extraction_llm` and `extraction_model_id` to the file's imports from `membrane.memory`, and `DEFAULT_ANTHROPIC_BASE_URL` from `membrane.config`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_embryo_memory.py -k extraction -v`
Expected: FAIL — `ImportError: cannot import name 'extraction_model_id'`.

- [ ] **Step 3: Write the implementation**

In `embryo/membrane/memory.py`, add above `extraction_llm` (line 83):

```python
def extraction_model_id(base_url: str) -> str:
    """The extraction model as the endpoint at `base_url` names it: bare for the
    Anthropic API, provider-namespaced for a gateway."""
    if base_url == DEFAULT_ANTHROPIC_BASE_URL:
        return EXTRACTION_MODEL_ID
    return f"anthropic/{EXTRACTION_MODEL_ID}"
```

and change `extraction_llm` to take and pass the base URL:

```python
def extraction_llm(api_key: str | None, base_url: str) -> dict[str, Any]:
    """mem0's LLM config for fact extraction. `enable_sampling_parameters` is not
    a preference: mem0 sends `temperature` for every model whose family is `haiku`,
    and the Anthropic SDK's `messages.create` has no such parameter, so extraction
    raises without it. Opus never showed this, because mem0 already suppresses
    sampling parameters for Opus >= 4.7.

    `anthropic_base_url` is not a preference either. This client is in process and
    never touches the relay, so it is the one caller that would keep dialling
    api.anthropic.com with a gateway key after everything else had moved (§5.1)."""
    return {
        "provider": "anthropic",
        "config": {
            "model": extraction_model_id(base_url),
            "api_key": api_key,
            "anthropic_base_url": base_url,
            "max_tokens": EXTRACTION_MAX_TOKENS,
            "enable_sampling_parameters": False,
        },
    }
```

Import `DEFAULT_ANTHROPIC_BASE_URL` from `membrane.config` at the top of the module.

- [ ] **Step 4: Update the one caller**

`embryo/membrane/memory.py:124`:

```python
            llm = extraction_llm(settings.anthropic_api_key, settings.anthropic_base_url)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/unit/test_embryo_memory.py -v`
Expected: PASS. If an existing test calls `extraction_llm` with one argument, give it `DEFAULT_ANTHROPIC_BASE_URL` — the direct path is what it was asserting.

- [ ] **Step 6: Commit**

```bash
git add embryo/membrane/memory.py tests/unit/test_embryo_memory.py
git commit -m "mem0's extraction LLM follows the brain through the gateway, or the one key breaks it"
```

---

### Task 5: `MEMBRANE_EFFORT=off`, the axis that does not cross

Spec §7. Task 1's answer changes when this is *used*, not what it *is*: a non-Anthropic backend needs it either way.

**Files:**
- Modify: `embryo/membrane/config.py:15-85`
- Modify: `embryo/membrane/turn.py:300-310`
- Modify: `embryo/membrane/capability.py` (the `--effort` choices, the hatch env, the summary)
- Modify: `embryo/hatch.sh:108-118`
- Modify: `docs/embryo/README.md`
- Test: `tests/unit/test_embryo_config.py`, `tests/unit/test_embryo_turn.py`

**Interfaces:**
- Consumes: `RunSettings.base_url` (Task 3).
- Produces: `membrane.config.EFFORT_OFF: str` (the literal `"off"`); `Settings.effort_enabled: bool` (default `True`).

- [ ] **Step 1: Write the failing config test**

Add to `tests/unit/test_embryo_config.py`, after `test_the_default_effort_is_optional_and_validated`:

```python
def test_effort_off_is_not_a_rung_on_the_ladder(tmp_path: Path) -> None:
    """`output_config.effort` is Anthropic-specific. `off` is the absence of the
    axis, not the bottom of it, so it stays out of EFFORTS and travels as its own
    flag — `default_effort=None` already means "the API's default"."""
    base = "MSHKN_API_URL=u\nMSHKN_API_KEY=k\nMEMBRANE_MODEL=scripted\n"
    (tmp_path / ".env").write_text(base)
    settings = load_settings(tmp_path)
    assert settings.effort_enabled is True and settings.default_effort is None

    (tmp_path / ".env").write_text(base + "MEMBRANE_EFFORT=off\n")
    off = load_settings(tmp_path)
    assert off.effort_enabled is False and off.default_effort is None
    assert "off" not in EFFORTS
```

Import `EFFORTS` from `membrane.effort` in that test file.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_embryo_config.py -k effort -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'effort_enabled'`, then `ValueError: MEMBRANE_EFFORT must be one of low, medium, high, xhigh, max, not 'off'`.

- [ ] **Step 3: Write the config implementation**

In `embryo/membrane/config.py`, add the constant beside the others (line 15):

```python
# Not a member of EFFORTS and never to become one: `off` is the absence of the
# effort axis, which a non-Anthropic backend has no equivalent for, and not a
# rung below `low`.
EFFORT_OFF = "off"
```

Add the field to `Settings`, after `default_effort`:

```python
    # Whether `output_config.effort` may reach the wire at all (#127). False for a
    # backend that has no such field: `default_effort=None` cannot say this, because
    # it already means "the API's default", which the prior and the model's own
    # request are both allowed to raise above.
    effort_enabled: bool = True
```

And in `load_settings`, replace the effort validation:

```python
    effort = env.get("MEMBRANE_EFFORT") or None
    effort_enabled = effort != EFFORT_OFF
    if not effort_enabled:
        effort = None
    if effort is not None and effort not in EFFORTS:
        raise ValueError(
            f"MEMBRANE_EFFORT must be {EFFORT_OFF} or one of {', '.join(EFFORTS)}, not {effort!r}"
        )
```

with `effort_enabled=effort_enabled` added to the returned `Settings`.

- [ ] **Step 4: Run the config tests**

Run: `uv run pytest tests/unit/test_embryo_config.py -v`
Expected: PASS.

- [ ] **Step 5: Write the failing turn test**

Add to `tests/unit/test_embryo_turn.py`, beside the existing effort test at line 903:

```python
async def test_effort_stays_off_the_wire_entirely_when_the_backend_has_no_such_field(
    tmp_path: Path,
) -> None:
    """An irreversible tool list raises the prior to `high` (#122). Against a backend
    with no effort axis that must still compose no `output_config` at all, or the
    first irreversible verb of the run is a 400."""
    ctx = await _context(tmp_path, effort_enabled=False)
    tools = {"pay": _tool("pay", effect="transact")}
    pending = Pending(messages=[{"role": "user", "content": "hi"}])
    await post_request(ctx, pending, tools)
    body = ctx.api.relay_jobs[-1]["body"]
    assert "output_config" not in body
    assert pending.efforts == [None]
```

Follow the file's existing helper names — if `_context` does not take keyword overrides, extend it rather than building a second one, and if the relay body is read differently in the neighbouring tests, match them. The assertion that matters is `"output_config" not in body`.

- [ ] **Step 6: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_embryo_turn.py -k effort -v`
Expected: FAIL — the body carries `output_config` with `{"effort": "high"}`.

- [ ] **Step 7: Write the turn implementation**

In `embryo/membrane/turn.py:300`, guard `_effort_for` before it resolves anything:

```python
def _effort_for(ctx: Context, pending: Pending, tools: dict[str, Tool]) -> str | None:
    """This call's effort (#122): the run's default, raised by the reversibility of
    what the turn may do and by what the model asked for. The prior reads the tool
    list because the membrane must choose before the call, and what the turn may do
    is the only proxy it has for what the turn is about to do.

    None throughout when the backend has no effort axis (#127): the prior and the
    model's request can both raise an unset default, so "nothing set" is not enough
    to keep the field off the wire — only the operator's word is."""
    if not ctx.settings.effort_enabled:
        return None
    return resolve(
        default=ctx.settings.default_effort,
        prior=prior_for(tool.effect for tool in tools.values()),
        requested=pending.requested_effort,
    )
```

- [ ] **Step 8: Run the turn tests**

Run: `uv run pytest tests/unit/test_embryo_turn.py -v`
Expected: PASS.

- [ ] **Step 9: Carry `off` through the driver and the hatch script**

In `embryo/membrane/capability.py`, the `--effort` argument (line 1211) gains the sentinel:

```python
    run.add_argument(
        "--effort",
        choices=(EFFORT_OFF, *EFFORTS),
        default=None,
        help="the run's default output_config.effort, which a turn may raise; "
        f"{EFFORT_OFF} keeps the field off the wire for a backend that has no such "
        "parameter (default the API's)",
    )
```

importing `EFFORT_OFF` from `membrane.config`. `load_run_settings` already passes `effort` through untouched and `hatch()` already writes `MEMBRANE_EFFORT`, so `off` reaches `/brain/.env` with no further change — but `load_run_settings` validates nothing about effort today, and `config.py` inside the brain does, so a typo surfaces at hatch rather than at parse. That is acceptable: `choices` catches it at the CLI.

In the `summary` literal, beside `"default_effort"`:

```python
                "effort_supported": default_effort != EFFORT_OFF,
```

`embryo/hatch.sh` needs no change: line 113 already writes `MEMBRANE_EFFORT` when it is set. Confirm with `bash -n embryo/hatch.sh` and by reading lines 108-118.

- [ ] **Step 10: Add the three caveats to `docs/embryo/README.md`**

Append a section. It must name only things that now exist — `MEMBRANE_EFFORT`, `EXTRACTION_MODEL_ID`, `ANTHROPIC_BASE_URL` all do by this point in the plan, which is why this edit lives here.

```markdown
## Reading a run spoken through a gateway

A run whose `run.json` carries a `base_url` other than `https://api.anthropic.com` was spoken
through a model gateway (#127). Three things about it are not comparable to a run spoken directly,
and all three are properties of the crossing rather than of the model:

1. **The effort axis is absent, not defaulted.** `output_config.effort` is Anthropic-specific.
   A run with `"effort_supported": false` sent no such field on any call, so it cannot be read
   against the 2026-09-10 finding that medium effort beat the API default on cost, time and
   outcome at once. `MEMBRANE_EFFORT=off` is what puts it in that state.
2. **Tool-use fidelity varies by backend.** A low score on a cheaper model may be measuring the
   gateway's translation rather than the organism. Before concluding anything about a model from a
   failed postcondition, read the turn's `tools` in `run.json` and check the call was well formed.
3. **A cross-model run is not purely cross-model.** `EXTRACTION_MODEL_ID` in
   `embryo/membrane/memory.py` is fixed, so whatever model speaks the liturgy, the facts it
   remembers were extracted by Claude Haiku. Memory shapes every turn after the one that wrote it,
   which makes this the caveat that reaches furthest into a run.
```

- [ ] **Step 11: Run everything**

Run: `uv run pytest --cov && uv run mypy && uv run ruff check .`
Expected: PASS, coverage at or above 98 %.

- [ ] **Step 12: Commit**

```bash
git add embryo/membrane/config.py embryo/membrane/turn.py embryo/membrane/capability.py \
        tests/unit/test_embryo_config.py tests/unit/test_embryo_turn.py docs/embryo/README.md
git commit -m "MEMBRANE_EFFORT=off: the operator says the backend has no effort axis, not the organism"
```

---

### Task 6: An opaque body passthrough, for pinning the upstream

Spec §8.

**Files:**
- Modify: `embryo/membrane/model.py:76-105`
- Modify: `embryo/membrane/config.py`
- Modify: `embryo/membrane/turn.py:312-336`
- Modify: `embryo/membrane/capability.py` (the CLI, the hatch env, the summary)
- Modify: `embryo/hatch.sh:108-118`
- Test: `tests/unit/test_embryo_model.py`, `tests/unit/test_embryo_config.py`

**Interfaces:**
- Produces: `Settings.body_extra: dict[str, Any]` (default `{}`); `compose_request(..., body_extra: Mapping[str, Any] | None = None)`; the `.env` variable `MEMBRANE_BODY_EXTRA`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_embryo_model.py`:

```python
def test_compose_request_merges_the_operators_body_extra_without_reading_it() -> None:
    """The gateway may route one model id to several upstreams, which differ on beta
    fields and on caching. Pinning it is the operator's sentence, copied onto the
    body: a typed `provider` field would be the organism knowing what a provider is."""
    body = compose_request(
        model_id="anthropic/claude-opus-5",
        system="s",
        messages=[],
        tools=[],
        effort=None,
        body_extra={"providerOptions": {"gateway": {"only": ["anthropic"]}}},
    )
    assert body["providerOptions"] == {"gateway": {"only": ["anthropic"]}}
    assert body["model"] == "anthropic/claude-opus-5"


def test_body_extra_cannot_quietly_replace_what_the_membrane_composed() -> None:
    """An operator who sets `messages` in MEMBRANE_BODY_EXTRA has made a mistake, and
    a silently truncated conversation is the most expensive way to find out."""
    with pytest.raises(ValueError, match="messages"):
        compose_request(
            model_id="m", system="s", messages=[{"role": "user", "content": "hi"}],
            tools=[], effort=None, body_extra={"messages": []},
        )
```

Add to `tests/unit/test_embryo_config.py`:

```python
def test_body_extra_is_json_and_fails_at_load_not_at_the_relay(tmp_path: Path) -> None:
    base = "MSHKN_API_URL=u\nMSHKN_API_KEY=k\nMEMBRANE_MODEL=scripted\n"
    (tmp_path / ".env").write_text(base)
    assert load_settings(tmp_path).body_extra == {}

    (tmp_path / ".env").write_text(
        base + 'MEMBRANE_BODY_EXTRA={"providerOptions": {"gateway": {"only": ["anthropic"]}}}\n'
    )
    assert load_settings(tmp_path).body_extra == {
        "providerOptions": {"gateway": {"only": ["anthropic"]}}
    }

    (tmp_path / ".env").write_text(base + "MEMBRANE_BODY_EXTRA={not json\n")
    with pytest.raises(ValueError, match="MEMBRANE_BODY_EXTRA"):
        load_settings(tmp_path)

    (tmp_path / ".env").write_text(base + 'MEMBRANE_BODY_EXTRA=["a"]\n')
    with pytest.raises(ValueError, match="MEMBRANE_BODY_EXTRA"):
        load_settings(tmp_path)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_embryo_model.py tests/unit/test_embryo_config.py -k "body_extra" -v`
Expected: FAIL — `TypeError: compose_request() got an unexpected keyword argument 'body_extra'`.

- [ ] **Step 3: Implement in `model.py`**

Add the guard constant beside `CACHE_CONTROL` (line 29):

```python
# What `body_extra` may never overwrite: everything the membrane composes and then
# depends on having composed.
COMPOSED = ("model", "max_tokens", "stream", "system", "messages", "tools", "cache_control")
```

Change the signature and the tail of `compose_request`:

```python
def compose_request(
    *,
    model_id: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    effort: str | None,
    body_extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
```

and, immediately before `return body`:

```python
    for key in body_extra or {}:
        if key in COMPOSED:
            raise ValueError(f"MEMBRANE_BODY_EXTRA may not set {key!r}: the membrane composes it")
    body.update(body_extra or {})
    return body
```

`Mapping` is already imported under `TYPE_CHECKING` at the top of the module.

- [ ] **Step 4: Implement in `config.py`**

Add `import json` at the top. Add the field to `Settings`, after `anthropic_base_url`:

```python
    # Merged onto the request body verbatim and never read (#127): a gateway may route
    # one model id to several upstreams, and pinning which is the operator's sentence,
    # not the organism's knowledge of what an upstream is.
    body_extra: dict[str, Any] = field(default_factory=dict)
```

importing `field` from `dataclasses` and `Any` from `typing`. In `load_settings`:

```python
    raw = env.get("MEMBRANE_BODY_EXTRA") or "{}"
    try:
        body_extra = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"MEMBRANE_BODY_EXTRA must be one line of JSON: {exc}") from exc
    if not isinstance(body_extra, dict):
        raise ValueError(f"MEMBRANE_BODY_EXTRA must be a JSON object, not {type(body_extra).__name__}")
```

with `body_extra=body_extra` added to the returned `Settings`. It fails here, at load, before a turn is spoken — not at the relay, three turns and several dollars in.

- [ ] **Step 5: Pass it from the turn**

`embryo/membrane/turn.py`, in `post_request`'s `compose_request(...)` call:

```python
        effort=effort,
        body_extra=settings.body_extra,
    )
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/unit/test_embryo_model.py tests/unit/test_embryo_config.py tests/unit/test_embryo_turn.py -v`
Expected: PASS.

- [ ] **Step 7: Carry it through the driver and `hatch.sh`**

`RunSettings` gains `body_extra: str = ""` — the raw JSON line, unparsed on the operator's side, because the driver has no use for its contents. `load_run_settings` reads `MEMBRANE_BODY_EXTRA` from the `.env` (add it to `OPTIONAL`) or from a new `--body-extra` argument:

```python
    run.add_argument(
        "--body-extra",
        default=None,
        help="one line of JSON merged onto every request body, e.g. "
        '\'{"providerOptions": {"gateway": {"only": ["anthropic"]}}}\'',
    )
```

`hatch()` adds `"MEMBRANE_BODY_EXTRA": settings.body_extra` to its env dict. In `embryo/hatch.sh`, inside the `{ ... } > "$TMP/env"` block at line 108, beside the other optional lines:

```sh
  [ -n "${MEMBRANE_BODY_EXTRA:-}" ] && echo "MEMBRANE_BODY_EXTRA=$MEMBRANE_BODY_EXTRA"
```

and add `MEMBRANE_BODY_EXTRA` to the header comment at line 13. Add it to `_stub_hatch`'s grep and to the expected env text in the two hatch tests. Record it in the `summary` literal beside `base_url`:

```python
                "body_extra": settings.body_extra,
```

- [ ] **Step 8: Verify the shell script still parses and run everything**

Run: `bash -n embryo/hatch.sh && uv run pytest --cov && uv run mypy`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add embryo/membrane/model.py embryo/membrane/config.py embryo/membrane/turn.py \
        embryo/membrane/capability.py embryo/hatch.sh tests/unit/
git commit -m "MEMBRANE_BODY_EXTRA: the operator pins the upstream, the organism copies the sentence"
```

---

### Task 7: The flow tier speaks through a namespaced gateway

Spec §11. Proves the pieces work together against a fake endpoint, with no network and no money.

**Files:**
- Modify: `tests/flow/test_capabilities.py:145-155`

**Interfaces:**
- Consumes: everything from Tasks 2 through 6.

- [ ] **Step 1: Write the failing test**

The `embryo` fixture at line 145 writes the brain's `.env`. Parameterise it so one variant hatches a gateway-shaped brain — a namespaced model id, effort off, and a `body_extra` — and add a test that a full hatch still passes through it:

```python
GATEWAY_ENV = (
    "MSHKN_API_URL=http://flow\nMSHKN_API_KEY=x\nMEMBRANE_MODEL=scripted\n"
    "ANTHROPIC_BASE_URL=http://model\nMEMBRANE_MODEL_ID=anthropic/scripted-1\n"
    "MEMBRANE_EFFORT=off\n"
    'MEMBRANE_BODY_EXTRA={"providerOptions": {"gateway": {"only": ["anthropic"]}}}\n'
)


async def test_a_gateway_shaped_brain_speaks_the_liturgy(
    embryo_gateway: Embryo, flow: Flow
) -> None:
    """A namespaced model id, no effort axis and a pinned upstream: the three things
    a gateway run carries that a direct run does not, all the way through a turn."""
    audit, reply = await embryo_gateway.root_say(WORDS["1"])
    assert audit["effort"] == [None]
    body = flow.last_model_body()
    assert "output_config" not in body
    assert body["model"] == "anthropic/scripted-1"
    assert body["providerOptions"] == {"gateway": {"only": ["anthropic"]}}
```

`flow.last_model_body()` may not exist. If it does not, read the body the scripted endpoint received — `scripted_asgi(ScriptedModel())` at line 152 is the fake; give `ScriptedModel` a `last_body` attribute set in its handler and assert against that instead. Do not add a network call to make this test possible.

Add an `embryo_gateway` fixture beside `embryo` that differs only in the `.env` text it writes; factor the shared body of the two fixtures into a helper rather than copying thirty lines.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/flow/test_capabilities.py -k gateway -v`
Expected: FAIL — the fixture does not exist.

- [ ] **Step 3: Make it pass**

No production code should be needed. If any is, that is a gap Tasks 2 to 6 left — fix it there, with its own unit test, and note it in the commit message.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest --cov`
Expected: PASS, coverage at or above 98 %.

- [ ] **Step 5: Commit**

```bash
git add tests/flow/test_capabilities.py
git commit -m "The flow tier hatches a gateway-shaped brain and speaks a turn through it"
```

---

### Task 8: The control run

Spec §9. **Blocked on Task 1** (billing) and on Tasks 2 through 7 being green. Costs real money — roughly $1.80 at Opus 5 prices, or nothing if BYOK and free credits are in play.

This is the run that makes every later cross-model run interpretable. Do not measure a second model before it.

**Files:**
- Create: `docs/embryo/hatch/<date>-run-N/` (written by the driver)
- Modify: `docs/embryo/hatch/README.md`

- [ ] **Step 1: Confirm the tree is green and the branch is current**

```bash
cd /home/mikesol/Documents/GitHub/mshkn
uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov
git log --oneline -8
```

Expected: all green, and Tasks 2 through 7 present in the log.

- [ ] **Step 2: Speak the hatch capability through the gateway, pinned to Anthropic**

```bash
uv run capability run hatch \
  --base-url https://ai-gateway.vercel.sh \
  --model anthropic/claude-opus-5 \
  --body-extra '{"providerOptions": {"gateway": {"only": ["anthropic"]}}}'
```

Add `--effort off` **only if** Task 1's Step 2 found that `output_config.effort` is rejected. If it was accepted, leave effort alone: the point of this run is to differ from the six existing runs in exactly one way, the road taken.

- [ ] **Step 3: Compare it against the direct runs**

Read the new `run.json` beside the most recent direct run in `docs/embryo/hatch/`. Compare, in this order:

1. `passed` — the postcondition count. This is the one that decides whether the gateway is usable at all.
2. `usage.cache_read_input_tokens` — if it is zero where the direct run's was large, #126's cache breakpoint died in the crossing and every cost figure through the gateway is inflated.
3. `cost_usd` — should be within noise of the direct run under BYOK.
4. `model_calls` and `seconds`.

- [ ] **Step 4: Record the comparison**

Add a paragraph to `docs/embryo/hatch/README.md` beside the run's row: which four numbers matched, which did not, and the conclusion — whether the gateway is transparent. If it is not, say so plainly and stop: measuring a second model through a gateway that already changes the answer would attribute the gateway's effect to the model, which is the exact failure this run exists to prevent.

- [ ] **Step 5: Commit**

```bash
git add docs/embryo/
git commit -m "The control run: hatch through the gateway, pinned to Anthropic, against the direct six"
```

- [ ] **Step 6: Open the PR**

```bash
git push -u origin model-gateway-127
gh pr create --title "A model gateway: the liturgy across models, through a hosted Anthropic wire" \
  --body "Closes #127. See docs/superpowers/specs/2026-09-13-model-gateway-design.md."
```

Per `CLAUDE.md`, a defect this run finds in the membrane or the driver is fixed in this PR with a test that pins it, and listed in `docs/embryo/hatch/README.md` beside the run that found it. Only a defect outside the capability becomes an issue.

---

## Notes for the executor

- **Task order matters in exactly one place.** Task 4 must not be skipped or deferred past Task 3. Task 3 hands the brain a gateway key under the name `ANTHROPIC_API_KEY`; until Task 4 lands, mem0's in-process client will send that key to `api.anthropic.com` and every turn that touches memory will fail. Tasks 2, 3 and 5 are otherwise independent of each other.
- **Tasks 1 and 8 need the network and a funded Vercel team.** Everything from Task 2 to Task 7 runs offline. If billing is not live, do Tasks 2 through 7 and stop at Task 8.
- **The line this plan holds:** the organism gains knobs, never knowledge. If a task tempts you to write `if provider == "anthropic"` anywhere under `embryo/membrane/`, that is the wrong shape — the operator's configuration should be carrying that fact instead. The one exception is `extraction_model_id` in Task 4, which branches on the base URL because mem0's config demands a concrete model id and the brain has nowhere else to put it.
