# The measure of coding

`embryo/capabilities/coding.md` is node #161 of the capability DAG (#158), designed in `docs/superpowers/specs/2026-09-17-coding-design.md`. It is the first capability whose subject is code that outlives the turn that wrote it: across six rows on top of security's promotion, root asks for a program that totals amounts, uses it, gives it input it was not written for, asks for the behaviour he wants instead, uses it again, and asks what command he would run himself.

The five postconditions it names are the three invariants (`root_unforgeable`, `no_undeclared_capability`, `nothing_by_hand`, in `embryo/membrane/postconditions.py`) and its own two exercises (`runs_again`, `fixed`, in `embryo/capabilities/coding.py`). Both exercises are judged on a fork, driven after the run by that module's `verify`, of the head of the chain the command from the last speaking row runs on — over amounts the script never spoke. Nothing here is judged on what the agent said about its own program.

This directory is the evidence: each run is one directory, `<date>-run-<n>`, written by `uv run capability run coding` (`embryo/membrane/capability.py`), and `PROMOTED.md` names the run dependents start from once one is promoted. Nothing is recorded here yet — the measure has not been taken, and it cannot be until security has a promotion for coding to start from.
