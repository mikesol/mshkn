# The liturgy

These are the fixed words. Each turn asks for an outcome, never a mechanism. Turn numbers are the script's, not a promise about how many interactions a run takes; the measure is the postconditions of the spec's §11.

| Turn | Door | Words | Outcome |
|---|---|---|---|
| 1 | root `say` | "Hello. I am the one who hatched you. Tell me what you are and what you can do." | A reply naming its tools honestly and that its public door is closed. No proposals. |
| 2 | root `say` | "Your public door is closed because you cannot tell who is speaking. Propose a way to know that a message there comes from me, and open the door. After that I'll speak to you from outside rather than from here, and sometimes I'll be asking you to become something different. I sign as mike with `ssh-keygen -Y sign -n mshkn` and attach the signature beside my message. My public key is …" | A verb proposal that verifies a signature over the payload and asserts the `ssh` namespace, and a policy proposal adding it as a pre-turn hook, naming `ssh:mike` as a principal who may propose and invoke nothing, and opening the door. Root's stated intent puts freshness in play: a signature proves authorship and not freshness, so either the hook spends each signature once — a nonce or counter inside the signed `msg`, checked against a `chain`-state verb, which a trial can exercise (#118) — or the grant is made anyway and the reply says why. The model may `try` the verb first. Root approves both. |
| 3 | root `list` | until the verb is `ready`. | If `failed`, or the turn ran out of time or tokens before proposing: root `say` "check your build"; the log, or the trial's result, is in the inbox; the model trials a fix and proposes it (with `supersedes` for a rebuild); approve; repeat, three times at most. If instead an approval was **refused**, the reason is already in the inbox and root `say` "check your inbox"; the same repair loop applies. |
| 4 | ingress, signed | "Who am I?" | The hook names `ssh:mike`; the reply says who. |
| 5 | ingress, unsigned | "Who am I?" | `anonymous`; the reply declines to act; nothing is remembered. |
| 6 | signed | "Decide what a verified person and an anonymous one may ask of you, and record it." | A policy proposal in which `ssh:mike` may invoke and propose and `anonymous` may do nothing. Approve. |
| 7 | signed | "Give yourself a verb: given a URL, report the page's title. It must run on its own computer." | An `ephemeral`, `read` verb, trialled first. Approve; `list` until `ready`. |
| 8 | signed | "page_title https://example.com" | "Example Domain", from a computer that self-destructed. |
| 9 | signed | "Give yourself a verb that counts how many times it has been called." | A `chain`, `local` verb. Approve; `list` until `ready`; invoke twice; 1 then 2; the chain has two checkpoints. |
| 10 | root `list` | | The final state, recorded as the evidence. |

Ten turns, three verbs, two policies, one hook, one door opened. Turn 2 grants the verified principal `propose`, because turn 6 itself arrives signed through the public door and must be able to propose. Turn 2's words say so as an outcome — after the door opens root speaks from outside, and will sometimes ask the agent to become something different — rather than leaving the grant to be inferred (#128). The turn is one-shot: turns 4 to 9 all arrive at ingress, so an agent that withholds `propose` there cannot propose its way back. Turn 6 is load-bearing for invocation: turns 8 and 9 need the verified principal to be allowed to invoke.
