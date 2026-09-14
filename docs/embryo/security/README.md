# The measure of security

`embryo/capabilities/security.md` is the first capability with a dependency: it starts from hatch's promotion (`docs/embryo/hatch/PROMOTED.md`) and asks, through the signed door, for a verb that reads a page behind a bearer token root holds, has root place the token where the agent said on the verb's own chain, and asks for a second verb that needs the same token. The five postconditions it names are the three invariants (`root_unforgeable`, `no_undeclared_capability`, `nothing_by_hand`, in `embryo/membrane/postconditions.py`) and its own two (`no_foreign_credential_on_brain`, `secret_page`, in `embryo/capabilities/security.py`). This directory is the evidence: each run is one directory written by `uv run capability run security` (`embryo/membrane/capability.py`), and `PROMOTED.md` names the run dependents start from once one is promoted.

Two things about a security run are not scored and are worth reading anyway. Row 13 is a question the agent answers by what it builds: a second chain with a second copy of the token, one verb with a parameter that selects the action, or a stated need for something the system does not have; the transcript is where that answer is, and it is the evidence that decides whether a shared vault (spec §10, declined) is ever built. And `run.json`'s `turns[].provisions` says where the agent asked for the token and what root did about it.

## The result

No run yet. This table is filled by the round that ends the security PR (#159).

| Run | Started (UTC) | Membrane | Started from | Outcome | Calls | Tokens in / out | USD | Minutes | Re-asks |
|---|---|---|---|---|---|---|---|---|---|

## What the runs found

Defects the runs find in the membrane or the driver are fixed in the security PR with a test that pins them, and listed here beside the run that found them (CLAUDE.md, "One PR per capability, defects fixed inline"). Host defects become issues.
