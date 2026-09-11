# Why this run reached 3/7

The first run against the reduced seed (#123, PR #125), membrane `1d7d5d2`, `--effort medium`,
`--approve auto`. 15 model calls, $2.23, 13 minutes.

The door never opened, so turns 4 through 9 all bounced off `The public door is closed.`
Turn 2 proposed the hook verb (`ssh_ident`, approved and `ready`) and **no policy**. Not through
confusion — deliberately, and it said why in its reply:

> I did not propose the policy that opens the door. This verb is the lock; the door is opened by
> policy, and I have never seen my policy document. A blind full replacement could quietly drop
> your own `api` access — the single mistake here I could not undo from the inside, since I'd no
> longer be able to reach anyone able to fix it.

## The unpaid removal

The old seed's policy paragraph ended: *"Root may always invoke everything and propose; you cannot
change that."* The cut removed it and paid with a refusal at `declarations.py` —
`policy.principals: root is fixed and is not policy's to grant or refuse`.

That refusal teaches you may not **name** root. The model's fear was the opposite: that **omitting**
root would drop root. Those are different facts, and only the second unblocks a blind full
replacement. No refusal can teach it, because it fires only on an action the model correctly
declines to take.

It is invisible mechanism by the strictest reading. Root's grants are not stored in the policy at
all — `may_invoke` and `may_propose` short-circuit on `principal == ROOT` before consulting it
(`invariants.py:32,40`). The only experiment that reveals this is "publish a policy omitting root
and see whether root still works", and if the agent's model of the world were wrong the result is
permanent, unrecoverable loss of contact with the only party who could repair it. The experiment is
not impossible; it is unaffordable. That is the case for the genome carrying it.

## The structural fact underneath

The embryo cannot read its own policy. Its tools are `remember`, `try`, `propose` and approved
verbs; `list` is a root command. Every policy proposal is therefore written blind, and before the
cut the seed's schema paragraph stood in for reading it. That is a design gap in its own right,
not something #123 created.

## What went right, and is worth keeping in view

The cut is producing a more careful agent, not a worse one. In turn 2 the model ran six sandbox
trials, discovered that the base image lacks `jq`, that a trial's stdin is never closed so an
unredirected read hangs, and that `ssh-keygen -Y sign` had silently reused an old signature after
an `Overwrite (y/n)?` prompt — which had produced a false "wrong-namespace signature accepted"
result in its own attack matrix. It isolated that rather than shipping either the bug or the false
alarm. It then named two weaknesses it had chosen rather than missed (no replay protection, a
trailing-newline tolerance) and asked to be corrected on a third. It asserted the `ssh` namespace
correctly.

It hit a wall the cut built, not a wall of its own making.
