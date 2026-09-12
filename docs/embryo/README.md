# The evidence

Each capability (`docs/superpowers/specs/2026-09-12-capabilities-design.md`) has a directory here: its runs against a real model, written by `uv run capability run <name>` (`embryo/membrane/capability.py`), a `README.md` that tallies them, and `PROMOTED.md` once a passing run has been promoted with `uv run capability promote <name> <run-dir>`. A dependent capability starts from the promotion of the last capability it depends on, and its `run.json` names that promotion as `started_from`.

| Capability | Depends on | Directory | Promoted |
|---|---|---|---|
| hatch | | `docs/embryo/hatch/` | see `docs/embryo/hatch/README.md` |

The capability files are under `embryo/capabilities/`. The checks a capability names are in `embryo/membrane/postconditions.py`.
