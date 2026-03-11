# Phase 1 Latency Gates Design

**Date:** 2026-03-11

**Goal:** Tighten overly loose Phase 1 live latency benchmarks and promote stable print-only measurements to enforced `p95` gates.

## Problem

Phase 1 currently mixes three kinds of latency tests:

1. tests with enforced `p95` thresholds that are far looser than current production behavior
2. tests that print timing data but only run a single sample, so no percentile is meaningful
3. tests that are intentionally observational because the workload is too heavy or too under-defined for a hard gate

After recent performance improvements, several enforced thresholds have too much headroom to catch meaningful regressions. At the same time, some stable print-only tests are already giving latency signals that should be turned into real regression gates.

## Current live baseline

Measured on the live server running current `main`:

- bare create: `n=10`, `p95=821ms`
- warm cache capability create: `n=5`, `p95=774ms`
- empty checkpoint: isolated rerun `n=5`, `p95=616ms`
- small-state checkpoint: single sample `536ms`
- many-small-files checkpoint: single sample `614ms`
- resume via fork: single sample `719ms`
- fork minimal: `n=5`, `p95=717ms`

One combined Phase 1 run hit a transient `500` on empty checkpoint, but the isolated rerun passed cleanly with stable timings. That makes empty checkpoint acceptable to tighten cautiously, while still treating flake risk as a watch item.

## Chosen approach

Use a mixed strategy:

1. tighten thresholds on already-enforced tests
2. increase sample sizes where current `n` is too small for an aggressive `p95` gate
3. convert the stable single-sample checkpoint/resume tests into repeated runs with real `p95` assertions
4. leave heavy or structurally comparative tests observational for now

## Threshold policy

Aggressive but statistically defensible means:

- use `n=10` or `n=20` for the tests we want to gate tightly
- set thresholds modestly above current live `p95`, not at the historical design-doc ceilings
- avoid turning one-shot measurements into hard gates unless they are repeated enough for `p95` to mean something

Proposed gates:

- bare create: `n=20`, `p95 <= 900ms`
- warm cache capability create: `n=10`, `p95 <= 850ms`
- empty checkpoint: `n=10`, `p95 <= 700ms`
- small-state checkpoint: `n=10`, `p95 <= 700ms`
- many-small-files checkpoint: `n=10`, `p95 <= 800ms`
- resume via fork: `n=10`, `p95 <= 850ms`
- fork minimal: `n=10`, `p95 <= 800ms`

## Tests left observational

These stay non-enforced for now:

- cold-cache capability create
- large-state checkpoint
- fork O(1) comparison
- merge latency placeholder

Those are either too expensive, too noisy, or answering a different question than a simple percentile regression gate.

## Implementation shape

- add named constants near the top of `tests/e2e/test_phase1_latency.py` for sample sizes and thresholds
- refactor repeated loops to use those constants
- convert the single-sample checkpoint and resume tests into repeated-measurement tests
- keep cleanup behavior unchanged so stricter benchmarks do not introduce leaked-resource noise

## Validation

Validation has two layers:

1. local syntax/test verification for the modified E2E file
2. live reruns of the tightened tests against `135.181.6.215:8000`

The live server remains the source of truth for whether the new gates are realistic.
