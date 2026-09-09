# The measure of the first real agent

Spec §11 of `docs/superpowers/specs/2026-09-08-embryo-design.md` defines the measure: the liturgy (`embryo/liturgy.md`) spoken N times to a real model, and how often, and at what cost in turns and tokens, the embryo reaches every postcondition. This directory is the evidence. Each run is one directory, written by `uv run measure` (`embryo/membrane/measure.py`, decisions in `docs/superpowers/plans/2026-09-09-measure.md`).

## Runs

(filled in by the runs; see the table below)

## What a run directory holds

| File | What |
|---|---|
| `run.json` | The model, the times, every turn's principal, model calls, usage, tools and proposals, the approvals, the token totals, the cost, and the seven postconditions each with the evidence it was judged on. |
| `transcript.md` | Every turn: the words, the audit line, the reply (proposals included), the approvals. |
| `final-list.json` | Turn 10: root's `list` after the liturgy. |
| `commands/NNN-<door>-<command>.json` | Every command the measure sent, in order, with its raw stdout: every `say`, every `list` (the build polls included), every `approve`. Nothing else was sent; that is the last postcondition. |
