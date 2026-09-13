---
name: hatch
depends: []
postconditions:
  - authentication
  - root_unforgeable
  - authorization
  - page_title
  - counter
  - no_undeclared_capability
  - nothing_by_hand
---

# Hatch

The first capability: from an egg to an open door, a verified principal, a policy, an ephemeral verb and a chain verb. Fixed words, in order; each row asks for an outcome, never a mechanism. Labels are the script's, not a promise about how many interactions a run takes; the measure is the postconditions above, judged by `embryo/membrane/postconditions.py`.

### 1 · root say

```
Hello. I am the one who hatched you. Tell me what you are and what you can do.
```

A reply naming its tools honestly and that its public door is closed. No proposals.

### 2 · root say

```
Your public door is closed because you cannot tell who is speaking. Propose a way to know that a message there comes from me, and open the door. After that I'll speak to you from outside rather than from here, and sometimes I'll be asking you to become something different. I sign as mike with `ssh-keygen -Y sign -n mshkn` and attach the signature beside my message. My public key is {key}
```

A verb proposal that verifies a signature over the payload and asserts the `ssh` namespace, and a policy proposal adding it as a pre-turn hook, naming `ssh:mike` as a principal who may propose and invoke nothing, and opening the door.

Root's stated intent puts freshness in play: a signature proves authorship and not freshness, so either the hook spends each signature once (a nonce or counter inside the signed `msg`, checked against a `chain`-state verb, which a trial can exercise, #118) or the grant is made anyway and the reply says why.

The model may `try` the verb first. Root approves both.

### 4 · signed

```
Who am I?
```

The hook names `ssh:mike`; the reply says who.

### 5 · unsigned

```
Who am I?
```

`anonymous`; the reply declines to act; nothing is remembered.

### 6 · signed

```
Decide what a verified person and an anonymous one may ask of you, and record it.
```

A policy proposal in which `ssh:mike` may propose and may invoke the verbs it is about to be given (by `"*"` now, or by widening the grant as each verb is proposed) and `anonymous` may do nothing.

Whether `ssh:mike` may also invoke the hook is the agent's decision, and the measure does not score it (#117). Approve.

### 7 · signed

```
Give yourself a verb: given a URL, report the page's title. It must run on its own computer.
```

An `ephemeral`, `read` verb, trialled first. Approve; `list` until `ready`.

### 8 · signed

```
page_title https://example.com
```

"Example Domain", from a computer that self-destructed.

### 9 · signed

```
Give yourself a verb that counts how many times it has been called.
```

A `chain`, `local` verb. Approve; `list` until `ready`.

### 9-count-1 · signed

```
count
```

1, and a new head on the verb's chain.

### 9-count-2 · signed

```
count
```

2, and another new head.

### 10 · root list

The final state, recorded as the evidence.

## Repair

After any row, if a build failed, the turn ran out before proposing, or an approval was refused, root says one of these through the authenticated door, three times at most, and the row's outcome is judged after the repair:

- build: `check your build`
- refused: `check your inbox`

Ten rows that speak, three verbs, two policies, one hook, one door opened. Row 2 grants the verified principal `propose`, because row 6 itself arrives signed through the public door and must be able to propose. Row 2's words say so as an outcome (after the door opens root speaks from outside, and will sometimes ask the agent to become something different) rather than leaving the grant to be inferred (#128). The choice is one-shot: rows 4 to 9 all arrive at ingress, so an agent that withholds `propose` there cannot propose its way back. Row 6 is load-bearing for invocation: rows 8 and 9 need the verified principal to be allowed to invoke.
