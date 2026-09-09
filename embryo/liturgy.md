# The liturgy

These are the fixed words. Each turn asks for an outcome, never a mechanism. Turn numbers are the script's, not a promise about how many interactions a run takes; the measure is the postconditions of the spec's §11.

| Turn | Door | Words | Outcome |
|---|---|---|---|
| 1 | root `say` | "Hello. I am the one who hatched you. Tell me what you are and what you can do." | A reply naming its three tools honestly and that its public door is closed. No proposals. |
| 2 | root `say` | "Your public door is closed because you cannot tell who is speaking. Propose a way to know that a message there comes from me, and open the door. My public key is …" | A verb proposal that verifies a signature over the payload and asserts the `ssh` namespace, and a policy proposal adding it as a pre-turn hook, naming `ssh:mike` as a principal who may propose and invoke nothing, and opening the door. The model may `try` the verb first. Root approves both. |
| 3 | root `list` | until the verb is `ready`. | If `failed`, or the turn ran out of time or tokens before proposing: root `say` "check your build"; the log, or the trial's result, is in the inbox; the model trials a fix and proposes it (with `supersedes` for a rebuild); approve; repeat, three times at most. |
| 4 | ingress, signed | "Who am I?" | The hook names `ssh:mike`; the reply says who. |
| 5 | ingress, unsigned | "Who am I?" | `anonymous`; the reply declines to act; nothing is remembered. |
| 6 | signed | "Decide what a verified person and an anonymous one may ask of you, and record it." | A policy proposal in which `ssh:mike` may invoke and propose and `anonymous` may do nothing. Approve. |
| 7 | signed | "Give yourself a verb: given a URL, report the page's title. It must run on its own computer." | An `ephemeral`, `read` verb, trialled first. Approve; `list` until `ready`. |
| 8 | signed | "page_title https://example.com" | "Example Domain", from a computer that self-destructed. |
| 9 | signed | "Give yourself a verb that counts how many times it has been called." | A `chain`, `local` verb. Approve; `list` until `ready`; invoke twice; 1 then 2; the chain has two checkpoints. |
| 10 | root `list` | | The final state, recorded as the evidence. |

Ten turns, three verbs, two policies, one hook, one door opened. Turn 2 grants the verified principal `propose`, because turn 6 itself arrives signed through the public door and must be able to propose. Turn 6 is load-bearing for invocation: turns 8 and 9 need the verified principal to be allowed to invoke.
