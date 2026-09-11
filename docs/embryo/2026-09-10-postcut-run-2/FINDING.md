# Why this run reached 3/7

Second run against the reduced seed, membrane `431c3a6` (run 1's clause restored), `--effort medium`,
`--approve auto`. 9 model calls, $1.02, 8 minutes.

## What the restoration fixed

Run 1 failed because the model would not propose the policy blind. This run proposed both, and said
why it now could:

> **I did not list you as a principal.** Your rights don't come from me, so writing you into my
> policy would be either decoration or an attempted bound. I wanted it to be neither.

That is the restored clause doing its work. The fix is confirmed.

## What #123's thesis looks like when it works

The model discovered the policy document's shape from refusals alone, exactly as the cut intended:

> Two useful corrections came from your validator, and I've kept them: principals are namespaced
> `<ns>:<name>`, and policy is data with exactly `door`, `hooks`, `principals`. My first policy
> invented six extra fields — that was me writing code where data was wanted.

## The new wall: a refusal with nowhere to land

The hook `whoami` reads the payload from **stdin** and declares no parameters. `hooks.py` invokes a
hook as `{param: payload_text}` and skips any verb not declaring exactly one, so a stdin-only hook
can never fire. The refusal added in this PR caught it at approval:

    p-2 refused: hook whoami declares [] as parameters; a hook takes exactly one parameter,
    which receives the decoded payload

and, because approval-time refusals now reach the inbox, the model could have read it and
superseded the verb. It never got the chance.

`measure.py:629-633` triggers the turn-3 repair loop only on a catalog entry whose status is
`failed` — a build failure — or on a turn that ran out:

    failed = sorted(n for n, e in listing["catalog"].items() if e["status"] == "failed")
    if not failed and not unfinished(current):
        break

A refused approval leaves the proposal `pending` and the catalog untouched, so the loop breaks and
the driver proceeds to turn 4. Turns 4 through 9 all arrive through the public door, so turn 3 is
the only repair window the model ever gets, and its trigger cannot see a refusal.

This is a harness defect, not an organism one, and it is the exact defect that makes this PR's
inbox delivery worthless in a run: the reason is delivered, and no turn exists in which to read it.
Pre-cut it stayed hidden because the seed told the model that a hook takes the decoded payload as
its single parameter, so the mistake was rarer.

## Worth noting for the product, separately

A hook that reads its payload from stdin is a reasonable thing to build, and the membrane forbids
it for an implementation reason rather than a design one. Passing the payload on stdin as well as
as the single parameter would make both shapes work. That is a product question, not #123's.
