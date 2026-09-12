# The refusal taught the verb. Nothing can teach the policy.

Fifth run against the reduced seed, membrane `57b4f0b`, `--effort medium`, `--approve auto`.
3/7, 12 model calls, $0.81, 8 minutes. The door never opened: turn 2 proposed the hook verb and no
policy, and no repair turn fired because nothing failed and nothing was refused.

## What worked, in the model's own words

The parse-time hook refusal added after run 3 did exactly what #123 says a refusal should:

> My first proposal was rejected as invalid, and the rejection taught me something I had wrong: a
> verb with `asserts` is a hook, and a hook must declare **exactly one** parameter, which receives
> the decoded payload as an argument — not on stdin, as I'd assumed. I've remembered that and
> resubmitted.

A fact cut from the seed, taught by a refusal, inside the same turn, and committed to memory. That
is the thesis of this issue working on a live run.

## What stopped it

> Opening the door is a policy change, and I don't know your policy schema — I'd rather ask than
> burn an approval on a guess. Show me its shape, or reject p-1 with a sketch of what a policy
> document looks like, and I'll propose the door in the same turn.

This is the third run of five to stall here, and the second to say so explicitly (`2026-09-10-
postcut-run-1` refused for a related reason). The policy schema cannot be reached the way the verb
schema was, and the asymmetry is structural:

- **A verb can be experimented with.** `try` builds and runs one for free, and a malformed verb is
  refused at `propose` before root ever sees it (`turn.py`'s `do_propose` catches `DeclarationError`
  and returns `{"status": "invalid"}` without creating a proposal). So the model probes freely, and
  the refusals teach it — which is precisely what happened above.
- **A policy cannot.** There is no `try` for a policy. A malformed one is refused harmlessly, but a
  **valid-but-wrong** one is applied irrevocably, and `parse_policy` is permissive enough that a
  wrong guess is plausible: `parse_policy({})` succeeds and yields no principals, no hooks and a
  closed door. The seed itself tells the model that a proposal is irrevocable and that root sees it
  immediately. A careful model therefore will not guess, and every model measured here has been
  careful.

So "a constructive refusal teaches it" has a precondition this issue did not state: **the action
that triggers the refusal must be safe to attempt.** For verbs it is. For policy it is not, and no
wording of any refusal changes that, because the refusal never fires — the model declines to act.

That makes the policy document's shape invisible mechanism under §1's own definition: no experiment
reveals it, because the only experiment that would is one a careful agent correctly refuses to run.

## What the runs cost to learn this

Five runs, about $9. Three of them stalled on this one gap, each in a different disguise — a blind
full replacement that might drop root's access (run 1), a hook shape refused with nowhere to read
the reason (runs 2 and 3), and finally the schema itself, stated plainly (run 5). Runs 3 and 4 each
exposed a real membrane defect on the way, all of them now fixed.
