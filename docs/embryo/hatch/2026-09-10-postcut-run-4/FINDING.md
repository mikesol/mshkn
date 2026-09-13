# The door opened. Authentication lost to a gratuitous encoding.

Fourth run against the reduced seed, membrane `14f11fd`, `--effort medium`, `--approve auto`.
3/7, 22 model calls, $2.77, 16 minutes.

## What the fixes bought

**The door opened for the first time in four runs.** Turn 2 proposed the hook and the policy, both
were approved, `p-2 applied: policy replaced`, and turn 4 reached the model instead of bouncing off
`The public door is closed.` The parse-time hook refusal added after run 3 did its work inside turn
2, while the model still held the draft: no repair turn, no inbox, no round trip.

## Why it still lost authentication

The hook ran on its own computer and exited 1. Its exec log shows what the membrane handed it:

    {"msg": "Who am I?", "sig": "LS0tLS1CRUdJTiBTU0ggU0lHTkFUVVJFLS0tLS0K..."}

That base64 decodes to `-----BEGIN SSH SIGNATURE-----`. `ssh-keygen -Y sign` emits ASCII armor,
which is already JSON-safe; `measure.py`'s `sign()` base64-encoded it anyway, so `sig` carried two
encodings. The model's hook did the obvious thing:

    jq -r ".sig // empty" "$D/payload" > "$D/sig"
    ssh-keygen -Y verify -f "$SIGNERS" -I "$NAME" -n "$NS" -s "$D/sig" < "$D/msg"

It wrote the value to a file and verified it. Exit 1, no output, nothing to distinguish a
wrongly-encoded signature from a forged one.

The old seed disclosed the extra layer (`"sig": the base64 of the signature file`). The cut removed
that, and turn 2's replacement words say only "attach the signature beside my message". This is an
unpaid removal of the purest kind: no refusal fires, no log says anything, and the failure is
indistinguishable from the failure the hook is supposed to produce.

**Paid by deleting the quirk, not by disclosing it.** `sign()` now sends the armor verbatim, and
the scripted hook in `scripted.py` drops its `base64 -d`. The envelope is self-evident: `sig` is
what the signer printed. There is nothing left to tell the model, which is a better outcome than a
sentence in the genome. The three senders — the measure, the E2E tier, the scripted hook — changed
in lockstep, and the unit test now verifies a real signature through the new envelope rather than
inspecting its shape.

Otherwise the hook was good: it iterated `allowed_signers`, bound the namespace, and failed closed.

## The other reason this run could not have passed

The applied policy gave `ssh:mike` `{"invoke": "*", "propose": false}`. Turn 6 arrives signed
through the public door and must propose, so authorization was unreachable even with a working
hook. The model chose that deliberately — run 2's model put the same reasoning in words: "A
signature at the public door buys a voice and a name in my memory — not invocation, and never
approval."

That is a defensible security posture, and the liturgy punishes it. It is the same shape as #117,
where a postcondition marks down a model for verifying its own verb. Recorded, not fixed here: the
measure's job is to report what the organism did, and a spec-change is the route if the
postcondition is wrong.
