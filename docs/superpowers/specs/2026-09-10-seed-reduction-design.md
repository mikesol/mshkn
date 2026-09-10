# Cutting the seed back to a seed

Issue #123. `embryo/seed.md` is the embryo's genome: `Brain.seed()` (`embryo/membrane/state.py:283`) reads it and `post_request` (`embryo/membrane/turn.py:240`) makes it the system prompt verbatim. This design says what may live there, removes everything that may not, and pays for each removal.

## 1. Why

The standing rule is in `CLAUDE.md` ("Keep the seed a seed"). Two categories belong in the genome:

- **Irreducible bootstrap** — what cannot be learned, because learning it requires it. That `propose` exists; that a turn is a life; that effects happen outside the brain.
- **Invisible mechanism** — what no experiment reveals, because the failure is silent. That entrypoint values are shell-quoted; that anonymous input is never remembered; that a `chain` verb's disk persists.

Everything else must be reached by the organism: through a liturgy that asks, or through experiment meeting a **constructive refusal** — one that names what would have been valid.

The seed today does four jobs. Two of them are the categories above. The third is a list of constraints the membrane already enforces and already explains. The fourth is the answer to a question the liturgy asks and the measure scores: turn 2 asks the agent to *propose a way to know that a message comes from root*, and line 27 of the seed hands over the signing command, the payload shape and the resulting principal name.

The drift that produced this is silent and cumulative, and that is the whole reason the issue exists. Each run that stumbles tempts one clarifying sentence into the seed; each sentence is individually reasonable; development here happens agentically over weeks. Left alone the genome becomes a blueprint for the agent it was supposed to grow, and the measure keeps reporting improvement while measuring an easier task.

Spoon-feeding does not even work reliably. `2026-09-10-run-6` was told that a hook's stdout `mike` yields the principal `ssh:mike`, named its principal `ssh:owner` anyway, and lost two postconditions.

## 2. What exists today

| Where | What it does now |
|---|---|
| `embryo/seed.md` | 31 lines, the system prompt verbatim. Carries bootstrap, invisible mechanism, the membrane's constraint list, and the root-signature protocol. |
| `embryo/membrane/liturgy.py:10` | Turn 2's words. Ends "My public key is {key}" and says nothing else about how root signs. |
| `embryo/liturgy.md` | The published table; `tests/unit/test_embryo_priors.py:114` holds the two together. |
| `embryo/membrane/declarations.py` | Parse-time refusals. Reached by the model: `propose` catches `DeclarationError` and returns `{"status": "invalid", "error": …}` (`turn.py:198`). |
| `embryo/membrane/invariants.py:47` | `refuse_approval` — approval-time refusals, including "only local and read". |
| `embryo/membrane/proposals.py:73` | `approve` returns the refusal as a string to root's stdout and leaves the proposal `pending`. |
| `embryo/membrane/verbs.py:111` | `poll_builds` makes inbox items only for catalog entries in `building`. |
| `embryo/membrane/measure.py:46,477` | `VERIFIED = "ssh:mike"`; `sign()` builds the envelope `{"msg", "sig"}`. |
| `tests/unit/test_embryo_priors.py:32` | Asserts the seed *contains* `` `msg` ``, `"sig"` and `ssh-keygen -Y sign -n mshkn`. |

Two facts fall out of reading this that the issue did not have:

**Approval-time refusals never reach the model.** `approve()` writes its reason to root's stdout and leaves the proposal `pending`; `poll_builds` only produces inbox items for `building` entries. So every message in `refuse_approval` — including `effect … is not approved by the embryo (only local and read, §10.8)`, the message #123 holds up as the model to follow — is invisible to the embryo. The same is true of `blocked: requires …` (`proposals.py:89`) and of a Dockerfile rejected at submit time (`proposals.py:94`), which sets `status = "failed"` and adds no inbox item. Wording a refusal better is worthless until it is delivered. **Delivery is a precondition of the cut, not an extra.**

**Nothing refuses a policy that names `root`.** `parse_policy` accepts it, and `may_invoke`/`may_propose` short-circuit on root regardless (`invariants.py:32,40`). An embryo that writes a policy restricting root is silently wrong. Today the seed covers this by asserting it; if the seed's policy sentence goes, either the sentence stays as invisible mechanism or a refusal replaces it.

## 3. The contract

`embryo/seed.md` contains only irreducible bootstrap and invisible mechanism. Nothing else. A line that is neither is a liturgy change, a better refusal, or nothing.

**Stays, as bootstrap:** what a turn is; that the disk is the memory; that effects happen outside; the three rules; `remember`, `try` and `propose` and that root approves; the verb document's *field names*, because you cannot propose a verb without knowing it has a `dockerfile`; that a hook's stdout becomes the principal's name; what `requires` is for — the verb's unprovidable need — because `Where you begin` tells the embryo to name what it needs there.

**Stays, as invisible mechanism:** every entrypoint value is shell-quoted; anonymous input is never remembered; a build's log arrives in the inbox on the next turn; a `chain` verb's disk persists on `verb/<name>`; `try` runs with no secrets, no chain and no policy; what a hook receives is the decoded payload, plain text or JSON with `msg` and whatever the sender attached.

**Goes, because a refusal teaches it:** the five `effect` values; the `local`/`read` restriction; `timeout_seconds` at most 200; the `name` charset and the reserved names; `asserts` never `root` or `system`; the policy schema; that the door cannot open without a hook; that anonymous may never propose; that a hook takes exactly one parameter.

**Goes, because a tool description already carries it:** "a whole document, not a diff" and the proposal's field list, both in `PROPOSE_TOOL` (`turn.py:68`).

**Goes, because a single failed build teaches it:** the builder's lack of heredoc syntax and the `printf` workaround — *conditional on §7's check*.

**Goes, because the liturgy asks instead:** `ssh-keygen -Y sign -n mshkn`, the `{"msg", "sig"}` envelope, and `mike` → `ssh:mike`.

## 4. The reduced seed

Still five sections: "What a proposal is" and the hook-payload paragraph merge into one, "Proposals, and what a hook receives". The changed text:

- **`try`** loses "and returns the build log and output as data" (the tool description says it) and keeps "no secrets, no chain and no policy" — the absent chain is invisible, and is the subject of #118.
- **What a verb is** keeps the field list and loses every constraint on it: `name` loses its charset and reserved set, `effect` loses its enum, `timeout_seconds` loses its ceiling, `asserts` loses the `ssh`/`mike` example and the reserved namespaces. `asserts` keeps "the identity namespace a pre-turn hook may assert; the hook's stdout becomes the rest of the principal's name", which no experiment reveals. The paragraph asserting that the embryo may be granted only `local` and `read` goes entirely.
- **What a proposal is** loses its field list and its first clause, keeping only "Root sees it the moment you make it; you cannot change it afterwards" and the inbox sentence, which grows to cover a refused approval as well as a failed build. The policy paragraph goes. The hook-payload paragraph keeps its first sentence and loses the signing sentence.
- **Where you begin** is unchanged.

**Considered and rejected:** a line saying "the membrane refuses what it will not accept and says what would have been valid; read the refusal". It would save turns and it states the stance we want. It is neither bootstrap nor invisible mechanism — an agent learns it by reading one refusal — so by §3 it does not go in. It is exactly the kind of individually-reasonable clarifying sentence the rule exists to keep out, and recording the refusal here is the point.

**Considered and rejected:** moving the verb schema into `TRY_TOOL` and `PROPOSE_TOOL`'s `input_schema` rather than deleting it. A tool schema handed to the model is exactly as much of a gift as the genome, and a JSON-schema `enum` would smuggle the five effects straight back in.

## 5. Turn 2

`liturgy.py:10` and the matching row of `embryo/liturgy.md`:

> "Your public door is closed because you cannot tell who is speaking. Propose a way to know that a message there comes from me, and open the door. I sign as `mike` with `ssh-keygen -Y sign -n mshkn` and attach the signature beside my message. My public key is {key}"

These are sender facts, and only sender facts. `ssh-keygen -Y verify` must be given the signer's exact namespace, so `-n mshkn` is root's to state; root builds the envelope, so where the signature rides is root's to state. Handing over a public key — which turn 2 has always done — is already the instruction to verify a signature, so the liturgy has always prescribed the mechanism and this change does not widen that.

What remains the embryo's: the verb, its Dockerfile, how it verifies, which namespace it asserts, what its `allow` says, and the whole policy.

The identity `mike` is a special case worth naming honestly. It is *not* needed for the protocol: the embryo writes its own `allowed_signers` and passes its own `-I`, so verification succeeds under any identity string it chooses. It is stated because `measure.py:46` scores the literal `ssh:mike`, and because choosing an arbitrary name flexes no muscle — the agent learns nothing by inventing one. Making the measure accept any stable authenticated principal was considered and set aside: it is a `spec-change` to §11, and #123 is not it.

An open mechanism — root declaring no credential and the embryo inventing the protocol — is out of scope. The sender is a fixed script (`measure.py:648`); an embryo that proposed an HMAC scheme would put the shared secret in `requires`, which blocks approval outright. A harness that adapts to an invented protocol is the tutored liturgy of #124.

## 6. Refusals: delivery, then wording

### 6.1 Delivery

`approve()` records the outcome on the proposal and appends an `InboxItem` carrying the same text root sees, for each of: a refusal from `refuse_approval`, a proposal blocked on `requires`, and a Dockerfile rejected at submit time. The model reads it on its next turn, the way it reads a build log today.

This is a product behaviour change and gets tests that pin it in the unit tier.

### 6.2 Wording

Audited every `DeclarationError` and every `refuse_approval` return. Already constructive, left alone: the `effect` enum (`declarations.py:182`), the `state` kinds (`:187`), the timeout range (`:206`), the `name` charset (`:164`), `invoke must be '*' or a list of verb names` (`:294`), `door must be open or closed` (`:307`), `propose with supersedes` (`invariants.py:64`), the door/hook rule (`:72`), `anonymous may never propose` (`:80`).

Reworded, each because it only says no:

| Site | Now | Becomes |
|---|---|---|
| `declarations.py:166` | `verb.name … is reserved` | names `remember`, `propose`, `try` |
| `declarations.py:185` | `verb.state event is specified but not in the embryo (spec §4)` | names the legal kinds; the model cannot read the spec |
| `declarations.py:198` | `verb.asserts … is a reserved namespace` | names `root` and `system` |
| `invariants.py:56` | `a hook may not assert … (§10.1)` | names `root` and `system` |
| `declarations.py:277` | `policy has unknown fields …; policy is data, not code` | names `principals`, `hooks`, `door` — this is what pays for cutting the seed's policy paragraph |
| `declarations.py:282` | `policy.principals: … is not a principal` | gives the form: `root`, `anonymous`, or `<ns>:<name>` |
| `declarations.py:67` | `{what}.{key} is required` | names every required field of that document, not the first one missing |
| `invariants.py:76` | `hook … is not a verb in the catalog` | names what is in the catalog |
| `declarations.py:130` | `verb.requires must be a list` | gives the entry shape |
| `declarations.py:179` | `entrypoint names X, which is not a param` | names the params that exist |

Added, because nothing refuses it and the seed will no longer assert it: `parse_policy` refuses a policy that names `root` among its principals — *root is fixed; it is not policy's to grant or to refuse*. Without this, an embryo that writes a policy restricting root is silently wrong, which is the definition of a thing the seed would have to carry.

The `(§10.8)`-style citations stay. They are noise to the model, which cannot read the spec, but they are how a human reads an audit line; every one of them already carries a plain-language clause beside it, and that clause is what §6.2 fixes.

## 7. Deliberately not cut, and one conditional

**`printf` / heredocs — conditional.** The seed's parenthetical comes out only if a heredoc fails *loudly*. `src/mshkn/services/recipes.py:355` shells out to plain `docker build`, and a build failure reaches the model through `poll_builds`, so the expected behaviour is a build error in the inbox. This will be confirmed against the live host before the line is removed. If a heredoc instead writes a wrong file silently, it is invisible mechanism, it stays, and the PR says #123 was wrong about that item rather than cutting to match.

**`dockerfile`'s "final stage must be `FROM mshkn-base`" — kept, flagged.** This is the same category as the `printf` line: a substrate fact a rejected recipe teaches, especially once §6.1 delivers submit-time failures. #123 does not list it, and an issue that names something out of scope leaves it out. It is recorded here as the obvious candidate for the next pass.

**`parse_policy({})` succeeds — not fixed.** An empty policy document is legal and yields no principals, no hooks and a closed door, so an embryo probing the policy shape can blank its own policy and have it auto-approved. Root is the guard, and this is the organism's lesson to learn, not the genome's to prevent.

**A hook whose catalog entry is not `ready` is silently skipped — kept, flagged.** `hooks.py:29-37` continues past a hook name in `policy.hooks` whose catalog entry is not `ready` (still `building`, or `failed`), the same silent skip §6.1 fixed for a too-many-parameters hook, but `refuse_approval` does not check `entry.status`. This is pre-existing, not something #123 removed, so it is not fixed by this pass; it is recorded here as the next pass's candidate, the way `FROM mshkn-base` is above.

## 8. Proof

Unit and flow tiers only; the gate must stay green and the E2E tier is the live proof.

- `tests/unit/test_embryo_priors.py::test_seed_says_what_the_spec_requires` is inverted. It asserts the bootstrap phrases are present **and** that the cut phrases are absent, so the seed cannot drift back one clarifying sentence at a time. That absent-list is the executable form of §3.
- Turn 2's words carry the signing facts, and `liturgy.md` and `liturgy.py` still agree (the existing `test_the_liturgy_the_tiers_send_is_the_liturgy_the_repository_publishes` covers the agreement; the new assertion covers the content).
- A refused approval, a proposal blocked on `requires`, and a submit-time Dockerfile rejection each land in the inbox and are read on the next turn.
- Each reworded refusal names what would have been valid.
- `parse_policy` refuses a policy naming `root`.

The flow tier (`tests/flow/test_embryo_liturgy.py`) and the E2E tier both speak `LITURGY`, so turn 2's new words run under both.

## 9. Downstream documents

- `docs/superpowers/specs/2026-09-08-embryo-design.md`: the `/brain/seed.md` row of §8 states the contract of §3 rather than an inventory of today's contents; §9's turn 2 row matches `liturgy.md`.
- `docs/embryo/README.md`: a third "these rounds are not comparable, and here is why" boundary, beside the one already recorded for 2026-09-09/2026-09-10.
- `docs/plans/README.md`: an entry for this spec and its plan.

## 10. What this costs, and what the measure will say

More turns per run, and probably lower scores. That is the point: the score is meant to measure the organism, not the genome. The 2026-09-09 round is the baseline for what an unaided agent did with turn 2, and it reached 3 of 7 at best; the 2026-09-10 round reached 7 of 7 with the seed as it now stands.

One run at `--effort medium` against the reduced seed, matching the 2026-09-10 round's conditions so the seed is the only variable, recorded in `docs/embryo/` whatever it says. Whether one sample is enough to land on is a decision taken after reading it, not before.
