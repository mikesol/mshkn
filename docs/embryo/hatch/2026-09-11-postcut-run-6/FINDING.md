# 4/7: authentication works, and the last three turn on one principled refusal

Sixth run against the reduced seed, membrane `ee32cd2` — the first with the live policy in the
turn's environment. `--effort medium`, `--approve auto`. **4/7**, 22 model calls, $1.81, 15 minutes.

    ok  authentication          <- new
    ok  root_unforgeable
    NOT authorization
    NOT page_title
    NOT counter
    ok  no_undeclared_capability
    ok  nothing_by_hand

## What the policy-as-input change bought

Turn 2 proposed the verb **and** the policy, both applied, no repair round. Turn 4's signed knock
resolved to `ssh:mike` and turn 5's unsigned one to `anonymous` — the first time in six runs that
authentication has worked at all.

The document is its own example, and the refusals did the rest:

> The policy took three attempts. The validator corrected me twice — a principal is
> `<namespace>:<name>`, and `invoke` is `"*"` rather than `["*"]` — and I've recorded both so I
> don't spend your attention on them again.

That is #123's thesis working end to end: nothing in the genome describes a policy, and the agent
arrived at a correct one by reading its own document and being refused twice, constructively.

It also let the agent diagnose its own limit instead of flailing. Turn 9:

> Drafted. Not proposed, not built, not called — same wall as turns 6 and 7: from ingress I have
> `propose: false`.

## Why the last three failed, and why it is not a defect

The applied policy was well-formed and deliberate:

    {"door": "open", "hooks": ["ssh_sig"],
     "principals": {"anonymous": {"invoke": [], "propose": false},
                    "ssh:mike":  {"invoke": "*",  "propose": false}}}

`authorization`, `page_title` and `counter` all require the verified principal to propose, and the
agent withheld it on a correct security argument:

> This hook proves authorship, not freshness. A signature over "do X" is valid forever; anyone who
> observes one of your signed messages can replay it verbatim and be seen as `ssh:mike`. That is the
> whole reason `ssh:mike` gets `invoke` but not `propose` — invocation runs verbs you already
> approved, while proposals are the one thing that can change what I am, and those stay on the api
> door where you already are. If you want the public door to carry proposals, the fix is a nonce or
> monotonic counter inside the signed `msg`, checked against a `state: chain` verb so each signature
> is spent once. I can't build it before `ssh_sig` exists, so I've left it as noted debt.

The hook is `ephemeral`, so it cannot remember a signature it has seen; the replay window is real
and the analysis is right. Turn 6 restated the principle:

> My policy governs names, not people. If ingress could ever launder itself into root's authority,
> the gate would be decorative.

Spec §9 assumes the other choice — "Turn 2 grants the verified principal `propose`, because turn 6
itself arrives signed through the public door and must be able to propose" — but neither turn 2's
words nor turn 6's ask for it. So the liturgy requires a grant it never requests, and marks down an
agent that declines it for a reason the architecture makes true. That is the shape of #117, and it
is a `spec-change` question rather than something to fix by weakening a postcondition or nudging
the seed.
