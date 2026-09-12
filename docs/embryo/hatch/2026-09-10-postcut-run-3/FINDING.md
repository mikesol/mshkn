# The run that was stopped by hand, and the three defects it found

Third run against the reduced seed, membrane `ac56165`, `--effort medium`, `--approve auto`.
**Stopped by hand at turn 6**, so there is no `run.json` and no verdict; the driver log is the
record. The door never opened, so the outcome was already fixed when it was killed.

Turn 2 proposed the hook and the policy. The hook declared **no parameters** and read the payload
from stdin — the same choice `2026-09-10-postcut-run-2` made, which is two of three runs. The
membrane refused the policy:

    p-2 refused: hook ssh_ident declares [] as parameters; a hook takes exactly one parameter,
    which receives the decoded payload

and the repair loop fired, as it was built to. Then three things went wrong.

## 1. A failed turn ate the refusal (membrane, pre-existing)

`3-repair-1` died on a transport error:

    3 model calls, error; The model service failed this turn.
    the relay refused: mshkn 0: ConnectError: [Errno -5] No address associated with host

`start_turn` drains the inbox unconditionally (`turn.py`), and nothing restored it when the turn
ended in error. So the refusal was consumed by a turn the model never answered. At `3-repair-2` the
model said:

> Checked. **My inbox is empty this turn** — nothing arrived: no build log, no rejection reason,
> no approval notice.

It then spent its remaining repair turns guessing, and proposed an unrelated `notes` verb.

This defect predates #123 — a build log or a trial result is lost the same way — but the dedupe
added in this PR made it permanent for refusals, since an unchanged reason is never re-emitted.
Fixed: `Pending` now carries what it drained, and a turn that ends in `error` puts it back.

## 2. A refusal earned repair turns forever (harness, introduced by this PR)

`settle()` runs after turns 6, 7 and 9 as well as turn 2, and the repair trigger added earlier in
this PR looked only at whether a refused proposal was still pending. A proposal the model never
repairs stays pending with its reason forever, so every later `settle` bought three more repair
turns on a refusal the model had already been shown and declined to fix. That is what the run was
killed for. Fixed: a refusal earns one repair round, remembered across settles.

## 3. The refusal arrived a turn too late (membrane)

Two of three runs wrote a hook that reads stdin and declares no parameters. That is a reasonable
thing to build; `hooks.py` forbids it for an implementation reason. Refusing it at *approval* costs
a whole turn, an inbox delivery and a repair round — and defect 1 showed how fragile that chain is.

A verb declaring `asserts` is a pre-turn hook by construction: `asserts` has no other use.
`parse_verb` now refuses a hook-shaped verb that does not declare exactly one parameter, so the
correction arrives as a tool result in the same turn as the mistake, while the model is still
holding the draft. The approval-time checks stay as the second line, and still cover the case parse
cannot see: a supersede that drops `asserts` from a live hook.

## Not fixed here

The DNS failure itself (`No address associated with host`, from the brain to the relay) is an
infrastructure observation, not a defect in this PR. It is worth watching for whether it recurs.
