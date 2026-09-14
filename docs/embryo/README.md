# The evidence

Each capability (`docs/superpowers/specs/2026-09-12-capabilities-design.md`) has a directory here: its runs against a real model, written by `uv run capability run <name>` (`embryo/membrane/capability.py`), a `README.md` that tallies them, and `PROMOTED.md` once a passing run has been promoted with `uv run capability promote <name> <run-dir>`. A dependent capability starts from the promotion of the last capability it depends on, and its `run.json` names that promotion as `started_from`.

| Capability | Depends on | Directory | Promoted |
|---|---|---|---|
| hatch | | `docs/embryo/hatch/` | [`2026-09-13-run-5`](hatch/PROMOTED.md) |
| security | hatch | `docs/embryo/security/` | not yet |
The capability files are under `embryo/capabilities/`. The checks a capability names are in `embryo/membrane/postconditions.py`; a capability names the invariants and its own exercises, not an ancestor's, because the ancestor's promotion is the proof those already passed (#167).

A capability's module (`embryo/capabilities/<name>.py`) may register checks only it needs; security's are `no_foreign_credential_on_brain` and `secret_page`.

## Reading a run spoken through a gateway

A run whose `run.json` carries a `base_url` other than `https://api.anthropic.com` was spoken
through a model gateway (#127). Four things about it are not comparable to a run spoken directly.
The first three are properties of the crossing rather than of the model; the fourth is a property
of how the crossing was paid for:

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
4. **`cost_usd` may be understated.** It prices a run off the local Anthropic price table
   (`embryo/membrane/capability.py`'s `PRICES`), which is only the gateway's actual bill under BYOK
   (`docs/infrastructure.md` marks the operator's Anthropic key in the gateway's BYOK settings
   optional). Nothing in `run.json` records whether BYOK was on for a given run, so a `cost_usd`
   from a non-BYOK gateway run sits next to the six direct runs looking comparable and is not. It is
   recoverable, though: `base_url` names the gateway, so a reader who suspects this can go check the
   dashboard for the key that ran it, which a run spoken directly never needs.