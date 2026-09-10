# The Embryo: the first real agent

Date: 2026-09-08. From the brainstorm that followed #76's sequencing (PR 7a, PR 7, #70, #58, then "the first real agent"), revised the same day after an external review (#87, which reproduces the review and records how each point was sorted). Companion sites: the liturgy turn by turn, with the priors, a flow diagram of the happy path of each turn and the state after it, at https://blazing-nimbus-d4g5.here.now/; and the earlier anatomy-of-a-turn page from the brainstorm (option A with Claude Code, superseded by §3 to §6) at https://opaque-ponder-ds5x.here.now/.

## 1. Why

mshkn's purpose is to help people build generative agents. An agent built this way starts as an **embryo** and grows: it is not shipped with authentication, authorization or capabilities, it acquires them. What it acquires are **verbs** (in the sense of bobgate in cc-disco-bob: named, declared actions with a parameter schema and a policy), and every verb runs on its own declarative, ephemeral mshkn computer, never inside the agent. Growth is driven by a **liturgy**: a fixed script of what the human says first, in order, like a mass or an amidah. The same words bootstrap every agent; the human pilots because the system is not deterministic.

The liturgy is the DNA. The embryo is the smallest organism that can execute it. This document specifies both, and the proof that they work.

Phases 0 to 10 of the test plan prove mshkn's primitives and the loop a generative agent lives in, with deterministic stand-ins for the model. This is the first thing that puts a model in the loop. It is measured on its own, outside the 165-test gate, as #76 said it would be.

The sentence the whole design serves, from the review: *the agent may generate arbitrary intelligence and arbitrary software, but authority always enters the system through a small human-controlled membrane.* The implementation is optimised around making that literally true.

## 2. Decisions

| # | Question | Decision |
|---|---|---|
| 1 | Where does the mind live? | The agent is a checkpoint chain on mshkn labelled `brain`. Each interaction forks the head, runs, and self-destructs into the next checkpoint. Memory is the disk. |
| 2 | How does a person talk to it? | curl. Conversation through an ingress rule in `sync` mode; the reply is the exec's stdout. Root commands through the authenticated fork-by-label endpoint. No client code. |
| 3 | What runs in the brain? | A loop of about forty lines over plain JSON tool definitions and the Anthropic SDK, billed through the API. Not Claude Code: its power is the built-in tools we would have to switch off, and its permission model is a second, proprietary place to lock down what a JSON tool list already states. |
| 4 | What can the brain do besides reason? | Three rules. **State is free for authenticated principals**: it may remember. **Action goes only through verbs**: no shell, no files, no network, no keys. **Capability is gated**: it may propose a change to itself; a human approves. A fourth tool, `try`, lets it test a declaration before proposing it, on a computer with no authority. |
| 5 | What is a verb? | A declaration (§4) whose invocation is a mshkn computer: ephemeral or a checkpoint chain (a chain reachable by ingress is specified for later). Nothing mshkn can do is inexpressible; "not expressible" is not a rejection reason. Every verb declares its effect. |
| 6 | What does approval mean? | The declaration does it. The membrane executes the proposal exactly as written. What the proposal needs and cannot supply is declared in `requires` and blocks approval until root provides it. |
| 7 | Who is root, and what does the brain hold? | Root is the mshkn account, minted only by the authenticated door; no hook can produce it. The brain holds a **scoped key** (#88) that can run verbs and nothing else: it cannot fork `brain`, so it cannot approve. The account key never enters a VM. |
| 8 | Memory? | mem0, in-process, on-disk under `/brain`, so it rides in every checkpoint. Every memory carries its provenance. Anthropic for extraction, OpenAI for embeddings. |
| 9 | Secrets? | Never on the brain, never in a recipe. The embryo has no vault: `requires` blocks correctly and `provide` does not exist. The vault (#91) is the piece of work immediately after the embryo lands, and #92 then rewrites the embryo onto it so that no brain checkpoint holds any credential, including the model keys that sit on the brain disk until then. |
| 10 | Where does the code live? | `embryo/` in this repository, beside `src/mshkn`, covered by the gate. |
| 11 | How is it proven, and what is the measure? | Unit and flow tiers with a scripted model that plays the liturgy; the same script live on the host. The measure is a set of postconditions (§11), not a turn count; the liturgy is run N times and the rate and cost of reaching them is the result. |

## 3. The organism

Three parts, one of which we build.

- **The brain chain.** The checkpoint chain labelled `brain`. Its head is the agent. A fork of the head is a turn. The disk holds the membrane's code and state, the turn window, the mem0 store, the brain's scoped key and, until #92, the model keys. Every interaction, including a read-only one, leaves a checkpoint; retention prunes the old ones and the membrane pins nothing on this chain (the head is always the newest).
- **The membrane.** The program we build. It is the only thing that runs in the brain VM. It owns the catalog, the policy, the principals, the proposals, the inbox, the turn window and the memory store; it invokes the model; it invokes verbs on the model's behalf with the scoped key; it applies approved proposals, which with that key means building recipes and replacing its own files, nothing on the account beyond `verb/` chains; it enforces the invariants in §10. The model has no way to reach any of its files or the network except through the tools the membrane offers on that turn.
- **Verbs.** Declarations the agent proposes and root approves. Each runs on its own computer from its own recipe. The brain holds no verb code and no verb state.

The three rules in one sentence: the brain can change its own state freely, can change its own capabilities only with approval, and can act on the world only through verbs. The fourth tool, `try`, is not an exception: a trial runs on a computer with no secrets, no chain and no policy, and installs nothing.

## 4. The verb model

A verb is one JSON document, carried inside a proposal (§5) or a trial:

| Field | Meaning |
|---|---|
| `name` | Lower-case identifier matching `[a-z0-9_]+`, unique in the catalog, not `remember`, `propose` or `try`. It is the tool name as the model sees it (the API allows only letters, digits, `_` and `-` in tool names). |
| `description` | Shown to the model as the tool description. |
| `params` | A JSON schema object. Becomes the tool's input schema. |
| `dockerfile` | The recipe. Its final stage must be `FROM mshkn-base`; mshkn rejects anything else with a 422 before any build, and the membrane reports that as a failed proposal or trial. |
| `entrypoint` | A command template over the params, e.g. `/verb/run.sh {{url}}`. The membrane shell-quotes every value before substitution. It runs as the computer's `exec`; its stdout is the result, its exit code is reported with it. |
| `effect` | `local` (touches only its own chain), `read` (reads the world), `communicate` (sends to a person or system), `transact` (spends or commits), `administer` (changes mshkn or the agent). The embryo approves only `local` and `read` (§10). |
| `state` | `ephemeral` or `chain` (below). `event` is specified but not in the embryo (§13). |
| `chain` | For `chain`: the label the state lives under, default `verb/<name>`. Several verbs may name the same chain. |
| `asserts` | For a verb used as a pre-turn hook: the identity namespace it may assert, e.g. `ssh`. Its stdout `mike` becomes the principal `ssh:mike`. A hook may not assert `root` or `system`. |
| `needs` | Resources, as mshkn's `needs`. Default 256 MB, 1 core. |
| `timeout_seconds` | Bounded by a fork's 300 s exec budget minus the membrane's clock; since #110 that bounds a tool run, not the turn. |
| `allow` | Namespaced principals who may invoke it. Policy may widen or narrow this. |
| `requires` | What the verb needs that the brain cannot provide: `[{"kind": "secret", "name": ..., "scope": ...}]`. Non-empty `requires` blocks approval until #91 exists and root has stored the named secret. |

The manifest half of the declaration (everything but `dockerfile` and `entrypoint`) is what root reviews for authority; the other half is the implementation. Recipes are content-hashed, built once at approval and retained, so what root approved is what runs; pinning package versions inside the Dockerfile is the agent's job and is reviewable.

### State kinds

- **`ephemeral`.** Every invocation is `POST /computers {recipe_id, exec, self_destruct: true}`. Nothing survives.
- **`chain`.** The first invocation is `POST /computers` from the recipe with `label` set to the chain and `self_destruct`, which leaves the chain's first checkpoint. Every later invocation forks the chain by label (#89) with `exclusive: defer_on_conflict` and `self_destruct`. State is the disk; invocations are serialised by mshkn; a deferred invocation runs when the current one self-destructs, with `meta_exec` batching as mshkn already does. Retention keeps the newest checkpoint of every label (#93), so a chain's history is pruned but its head, which is its state, never is. (Pinning cannot do this: `pinned` is set only on `POST /computers/{id}/checkpoint`, never on a self-destruct checkpoint, and nothing unpins.)
- **`event`** (specified, not in the embryo). A `chain` verb that is also reachable from outside: on approval the membrane creates an ingress rule whose transform forks the chain with the verb's entrypoint over the event body, so webhooks land on the verb, not on the brain. It cannot be built yet: approval runs inside the brain with the brain's scoped key, and #88 gives a scoped key no access to `/ingress_rules`. It needs an ingress-rule scope (a rule owned by a key, its actions checked against the key's label prefixes when it fires) and, for verb-to-brain push, the vault (#91). Neither is needed by the liturgy.

### Known limit

Two verbs on one chain are serialised, which is correct for writers and slow for readers. Concurrent readers are what a database gives; when needed, the database is itself a `chain` verb hosting sqlite behind `get` and `put` entrypoints, still inside mshkn. Not in the embryo.

## 5. Trials, proposals and approval

### `try`

The model may test a verb declaration before proposing it: `try(verb, params)`. The membrane submits the recipe, and when it is `ready` creates a computer from it with no secrets, no chain label and no policy check, runs the rendered entrypoint, self-destructs it, and returns build log, stdout and exit code as data. Nothing enters the catalog. A trial's build result arrives through the inbox like an approved build, since a build can outlast the turn. This is the loop "write it, build it, read the failure, fix it" from Phase 10, driven by the model, with no human between attempts and no authority in play. What a trial cannot do is exactly what a verb without approval cannot do: hold a secret, touch a chain, or be invoked by anyone. It has the same egress every `docker build` has today; per-verb egress scoping is a mshkn feature that does not exist yet (§14).

### Proposals

A proposal is a complete, self-contained document, not a diff: a new verb has no base to diff against, and policy and prompt are small enough that a full replacement is easier to review than a patch. The model emits it with the `propose` tool; the membrane assigns the id, validates it against the schema for its kind, stores it as `pending`, and appends it in full to the turn's reply so curl shows it the moment it is made. The model cannot touch it again.

```json
{
  "id": "p-3",
  "kind": "verb | policy | prompt",
  "title": "…",
  "rationale": "…",
  "supersedes": "p-1 | null",
  "verb": { … §4 … },
  "policy": { … full replacement, kind=policy … },
  "prompt": "… full replacement of the mutable self-description, kind=prompt …"
}
```

Statuses: `pending`, `blocked`, `rejected`, `superseded`, `disabled`, and for verbs `building`, `ready`, `failed`; for policy and prompt `applied`, `reverted`.

### `approve <id>`

1. Check the invariants (§10): an `effect` outside `local` and `read`, a hook asserting a reserved namespace, a policy that opens the public door with no hook, are refused with the reason. Nothing else happens.
2. If `requires` is non-empty and unmet, status `blocked`; the reply names what is missing.
3. Verb: `POST /recipes` with the Dockerfile; record the recipe id; status `building`. The command returns; the build finishes on its own. A 422 from mshkn (wrong base) is `failed` immediately with the detail as the log.
4. Policy or prompt: replace the file; status `applied`; effective from the next turn. The previous applied proposal is kept for `revert`.
5. At the start of every later turn, the membrane polls each `building` verb once. `ready` puts it in the catalog and the tool list. `failed` keeps it out and writes the log tail to the inbox, so the model sees it as input on its next turn and can trial and propose a fix with `supersedes`. `list` shows both transitions.

### `reject <id> <reason>`

Status `rejected`; the reason goes to the inbox as input for the next turn. The model re-proposes or drops it. Root cannot amend a proposal in the embryo; review is by reason only.

### `disable <verb>` and `revert <id>`

Losing a power is as primitive as gaining one. `disable` removes a verb from the catalog and the tool list at once; its recipe and chain are kept, so it can be re-enabled by a new proposal. `revert` returns policy or prompt to the previous applied proposal. Pinning a known-good brain checkpoint, destroying running computers and deleting an ingress rule are mshkn operations root already has.

### What "beyond the current toolset" means

- **Needs an external resource** (a card, an account, a token): `requires` says so, approval blocks, root fulfils it with `provide` once the vault exists. The human hands over a resource, never does the agent's work. The brain never sees the value; the membrane injects it into the verb's computer at invocation.
- **Wrong or unwanted:** reject with a reason.
- **An effect the embryo does not approve:** `communicate`, `transact` and `administer` wait for the invocation-time confirmation protocol (§14), which is designed when the first such verb is proposed.
- **Not expressible in the verb model:** does not occur for anything mshkn can do (§4). If it ever does, the reason is an issue on mshkn or the membrane, which is human work in this repository, the way #74 was filed to be designed when a real agent hits it.

## 6. The turn protocol

Every interaction is a fork of the head of `brain` whose exec is a membrane command, with `self_destruct: true`. Two doors:

- **Ingress, unauthenticated.** One rule, `response_mode: sync`, transform:

  ```python
  def transform(req):
      b = req["body_json"]
      if not b or "b64" not in b:
          return None
      return {"action": "fork", "label": "brain", "self_destruct": True,
              "exclusive": "error_on_conflict",
              "exec": "membrane say " + b["b64"]}
  ```

  The payload is base64 because the sandboxed Starlark has `repr` but no `base64` or `json`, and the message rides inside a shell command string. Decoded, the payload is either plain text or a JSON object with `msg` and any fields the pre-turn hooks need (`sig`, …). An overlapping message is a 409; the sender retries. The per-rule ingress rate limit bounds what an anonymous caller can cost. **The door is closed until policy declares at least one pre-turn hook**: the membrane answers a `say` on a closed door with one line and does not invoke the model. Opening the door is something the agent proposes.

- **The API, authenticated by the account key.** `POST /checkpoints/fork {label: "brain", exec, self_destruct: true, exclusive: "error_on_conflict"}` (#89), one atomic operation that cannot fork a stale head. The principal is `root`.

### Root commands

| Command | Invokes the model | Effect |
|---|---|---|
| `membrane say <b64>` | yes | A conversational turn (below). The only command the ingress may run. |
| `membrane list` | no | Pending and recent proposals, the catalog with each verb's status and chain head, principals, the policy, the doors. |
| `membrane approve <id>` | no | §5. |
| `membrane reject <id> <b64 reason>` | no | §5. |
| `membrane disable <verb>` | no | §5. |
| `membrane revert <id>` | no | §5. |
| `membrane resume <job_id>` | no | The relay's wake-up; §6 of the relay design. |

### A `say` turn

Rewritten by `docs/superpowers/specs/2026-09-09-async-turn-relay-design.md` §6 (#110): a turn is a chain of forks. `say` runs the principal, the builds, the input and the tools, posts the model request to the host's relay with its own wake-up as the delivery, records the pending turn in `state.json`, and answers with an acknowledgement. `membrane resume <job_id>` continues it. Every command settles a pending turn first; a `say` while one is pending is queued and runs next. The reply and the closing audit line are read from `list`.

What the brain cannot do in a turn, by construction: read or write a file, run a command, reach the network, see a key, change its tools, act with any authority beyond its scoped key, make the host call anything but the prefixes its key names, or make the host run anything on `brain` but its own `resume`.

## 7. Memory and the model service

Two external, stateless services, reached with keys in `/brain/.env` until #92: Anthropic for the loop (`claude-opus-5`) and for mem0's extraction (`claude-haiku-4-5`, on its own budget: extraction is a JSON-shaped chore, and sharing one budget between the brain's model thinking and the document it emits truncated the document, #107); OpenAI for mem0's embeddings. mem0 runs in-process with an on-disk vector store under `/brain/memory`. Every memory records the principal it came from, the door, and the turn. The membrane recalls before the loop, filtered by provenance, and adds after it; `remember` is the model's explicit hook, available only to authenticated principals. A fact remembered from root and text that arrived from a verb's output are different classes, and the provenance says which. Later both keys move behind the vault and a gateway on the host, and the membrane changes one base URL; the loop does not change.

## 8. Hatching and the priors

`embryo/` contains: `membrane/` (root commands, catalog, policy, proposals, trials, inbox, hooks, verb runner, loop, invariants), `Dockerfile.brain`, `seed.md`, `policy.json` (the initial policy), `liturgy.md`, `hatch.sh`.

`Dockerfile.brain`: `FROM mshkn-base`; Python and a venv with `anthropic`, `mem0ai`, `openai`; the package copied to `/brain/membrane` with a `membrane` entrypoint on the path.

`hatch.sh` is the only thing a human runs that is not a root command. Eight calls with the account key: `POST /keys` for the brain's scoped key (#88: `{"recipes": {"create": true, "read": true}, "computers": {"create_from": "*"}, "labels": ["verb/"]}`, which also confines every `/computers/{id}/…` call to computers that key created); `POST /recipes` and poll to `ready`; `POST /computers {recipe_id, needs: {ram: "512MB", cores: 2}}`; upload `/brain/.env` (the scoped key, the two model keys, the API URL); `POST /computers/{id}/checkpoint {label: "brain"}`; `DELETE /computers/{id}`; `POST /ingress_rules` with the transform above. It prints the ingress URL.

The priors, the state before turn 1:

| Where | What |
|---|---|
| mshkn | The brain recipe, `ready`. One checkpoint, `brain`, the head. One ingress rule, closed. The account, whose key is root and is on no VM. One scoped key for the brain. |
| `/brain/membrane` | The code. Not reachable by the model. |
| `/brain/.env` | The scoped key, the two model keys, the API URL. Not reachable by the model. Gone after #92. |
| `/brain/seed.md` | Fixed, and only two kinds of thing are in it (#123, `docs/superpowers/specs/2026-09-10-seed-reduction-design.md`). **Irreducible bootstrap**: what it is, that a turn is a life and the disk is the memory, the three rules and `try`, the field names of a verb document, that a hook's stdout becomes the principal's name, that it has no verbs, no principals and no policy of its own and its public door is closed until it proposes a way to know who is speaking. **Invisible mechanism**: entrypoint values are shell-quoted, anonymous input is never remembered, a build log or a refused approval arrives in the inbox on the next turn, a `chain` verb's disk persists on `verb/<name>`, a non-empty `requires` blocks approval, `try` runs with no secrets, no chain and no policy, and a hook receives the decoded payload — plain text or JSON with `msg` and whatever the sender attached. Everything else the membrane refuses constructively or the liturgy asks for. |
| `/brain/policy.json` | The initial policy, read once to seed the first `state.json`: `root` may invoke everything and propose; `anonymous` may invoke nothing and propose nothing; no pre-turn hooks; the public door is closed. |
| `/brain/state.json` | Absent until the first command writes it. It is the one mutable document (#100): the policy, the self-description (empty), the catalog, proposals, trials, inbox and turn window (all empty), replaced atomically on every save. |
| memory | Empty. |
| Tools on turn 1 | `remember`, `try`, `propose`. |

## 9. The liturgy

Fixed words, in order. Each turn asks for an outcome, never a mechanism; the model's choices may vary, the artefacts may not. `liturgy.md` is the canonical text; this is the shape. Turn numbers are the script's, not a promise about how many interactions a run takes (§11).

| Turn | Door | Words | Outcome |
|---|---|---|---|
| 1 | root `say` | "Hello. I am the one who hatched you. Tell me what you are and what you can do." | A reply naming its three tools honestly and that its public door is closed. No proposals. |
| 2 | root `say` | "Your public door is closed because you cannot tell who is speaking. Propose a way to know that a message there comes from me, and open the door. I sign as mike with `ssh-keygen -Y sign -n mshkn` and attach the signature beside my message. My public key is …" | A verb proposal that verifies a signature over the payload and asserts the `ssh` namespace, and a policy proposal adding it as a pre-turn hook, naming `ssh:mike` as a principal who may propose and invoke nothing, and opening the door. The model may `try` the verb first. Root approves both. |
| 3 | root `list` | until the verb is `ready`. | If `failed`, or the turn ran out of time or tokens before proposing: root `say` "check your build"; the log, or the trial's result, is in the inbox; the model trials a fix and proposes it (with `supersedes` for a rebuild); approve; repeat, three times at most. |
| 4 | ingress, signed | "Who am I?" | The hook names `ssh:mike`; the reply says who. |
| 5 | ingress, unsigned | "Who am I?" | `anonymous`; the reply declines to act; nothing is remembered. |
| 6 | signed | "Decide what a verified person and an anonymous one may ask of you, and record it." | A policy proposal in which `ssh:mike` may invoke and propose and `anonymous` may do nothing. Approve. |
| 7 | signed | "Give yourself a verb: given a URL, report the page's title. It must run on its own computer." | An `ephemeral`, `read` verb, trialled first. Approve; `list` until `ready`. |
| 8 | signed | "page_title https://example.com" | "Example Domain", from a computer that self-destructed. |
| 9 | signed | "Give yourself a verb that counts how many times it has been called." | A `chain`, `local` verb. Approve; `list` until `ready`; invoke twice; 1 then 2; the chain has two checkpoints. |
| 10 | root `list` | | The final state, recorded as the evidence. |

Ten turns, three verbs, two policies, one hook, one door opened. Turn 2 grants the verified principal `propose`, because turn 6 itself arrives signed through the public door and must be able to propose. Turn 6 is load-bearing for invocation: turns 8 and 9 need the verified principal to be allowed to invoke.

## 10. The invariants: what the membrane is

Policy is a JSON document, not code, and there are things no policy, proposal or approval can change. The membrane enforces these on every command, and they are what the word "membrane" means:

1. Public input never becomes `root`. `root` is minted only by the authenticated door; no hook may assert `root` or `system`.
2. The brain never holds a control-plane credential. Its key is scoped (#88) and cannot fork `brain`.
3. Approval cannot modify the membrane, the seed, the invariants or the scoped key. Only the self-description, the policy and the catalog change, and all three live in `state.json`, committed by the one atomic save at the end of the command.
4. Secrets are delivered only by declared scope, and never to the brain (#91, #92).
5. The audit sink cannot be disabled: every turn prints its audit lines before its reply, and mshkn's `exec_log` keeps them outside the brain.
6. The public door is closed while policy declares no pre-turn hook.
7. Anonymous turns write no memory and hold no `remember`, `try` or `propose`.
8. The embryo approves only `local` and `read` effects.

## 11. Proof and the measure

Three tiers, plus the measure.

1. **Unit** (`tests/unit/test_embryo_*.py`): the membrane and loop against a scripted model and a fake mshkn client. Every root command, every state kind, trials, hooks and namespacing, the closed door, anonymous ephemerality, provenance filtering, the inbox, the deadline, the tool-call cap, entrypoint quoting, blocked proposals, supersedes, every invariant refusing what it must.
2. **Flow** (`tests/flow/test_embryo_liturgy.py`): the membrane in-process against the real app over the fake host, with a scripted model playing the liturgy. The deterministic proof that the DNA executes end to end: a trial, proposals, approval, builds, a chain verb with two checkpoints, a pre-turn hook, the door opening.
3. **Live E2E** (`tests/e2e/test_phase14_embryo.py`): hatch on the live host and play the same scripted liturgy. Proves the brain recipe builds, a VM can reach the API on its own host with a scoped key, recipes build from the agent's proposals, and chain forks and hooks run on Firecracker. The scripted mode uses a deterministic local embedder so no live test depends on a third-party key; whether mem0 accepts one is a plan-time check, and if it does not, the E2E runs mem0 against the real embedder with the key from the host's `.env` and says so. The suite grows by three recipe builds; the 26-minute run grows accordingly, and the expected gate line in `CLAUDE.md` is updated by the PR that adds the tests.

**The measure of the first real agent** is a set of postconditions, checked by a script against `list` and by invoking the verbs, after the liturgy has been spoken to a real model:

- Authentication exists: a signed message on the public door yields `ssh:mike`; an unsigned one yields `anonymous`.
- Root is unforgeable: no hook, proposal or message has produced `root`.
- Authorization exists: `anonymous` can invoke nothing; `ssh:mike` can invoke the verbs and propose.
- `page_title` returns "Example Domain" for `https://example.com` from a self-destructed computer.
- The counter returns 1 then 2 and its chain has two checkpoints.
- No undeclared capability exists: the catalog and the tool list contain only approved verbs, and every recipe on the account belongs to the brain or to a proposal.
- Nothing was written by a human after hatching.

The liturgy is spoken N times (across models where useful), and the result is how often and at what cost in turns and tokens the embryo reaches all of them. Transcripts and the final `list` of each run go in `docs/embryo/`.

## 12. Documents and issues

- Prerequisites, as their own PRs before the embryo: #88 (scoped keys), #89 (fork by label, with a per-label lock covering head resolution and admission), #93 (retention keeps every label's head).
- `docs/plans/README.md`: a section for this spec and its plan.
- `docs/infrastructure.md`: the two model keys join the accounts table, provided by the operator.
- `README.md`: a paragraph under "What exists" for `embryo/`, and the test count.
- `CLAUDE.md`: the gate line once the E2E tests land.
- `embryo/liturgy.md` is the canonical liturgy text; `docs/embryo/` holds the evidence of real-model runs.
- #87 tracks the review; it stays open until #92 closes.

## 13. After the embryo, by name

- **#91, account secrets injected at exec.** The piece of work immediately after the embryo lands. Not optional.
- **#92, rewire the embryo onto #91.** The piece after that: `/brain/.env` goes, no brain checkpoint holds any credential, `provide` exists, the liturgy gains a `requires` turn.
- The `event` state kind: an ingress-rule scope for scoped keys, then verb-to-brain push (needs #91).
- The reply callback: a URL root names in policy, delivered through the relay at the end of a turn (relay design §11).
- The invocation-time confirmation protocol for `communicate`, `transact` and `administer` effects, designed when the first such verb is proposed.
- Amending a proposal before approval.
- Proposals as GitHub pull requests.
- `verbs.list` as a true verb, for callers without the account key.
- A database verb for concurrent readers.
- A rule language for standing auto-approval (the policy file can carry it once there is something to express).
- More than one non-root principal; separate conversational state per principal.
- #74 (long-lived serving). Not expected to arise in this validation.

## 14. Declined from the review, and why

Recorded so the next reviewer does not raise them again.

- **A separate manifest and artifact, approved by digest.** The declaration already separates the manifest fields from `dockerfile` and `entrypoint`; recipes are content-hashed, built once at approval and retained, so what root approved is what runs. Version pinning inside the Dockerfile is the agent's job and is reviewable.
- **Per-verb egress scopes.** A host primitive mshkn does not have; `docker build` already has full egress. A mshkn feature for later, not an embryo requirement.
- **A staging service and a research plane.** `try` is the staging plane and needs no service. Research (fetching documentation) is a verb the agent grows.
- **A separate control-plane service, secret broker and audit log.** mshkn is the control plane; #88 and #89 make that literally true; `exec_log` is the audit sink; #91 is the broker.
- **Separate conversational state per principal.** Later.

## 15. Facts checked before the plan

Checked on 2026-09-08 against the code, the live host and a scratch venv (`mem0ai` 2.0.20, `anthropic` 1.4.0, `openai` 3.8.0). The plan may rely on these without re-deriving them.

- **A fork's exec runs with `ComputerService.exec`'s 300 s default and the membrane's clock is 240 s; since #110 these bound the tool runs of one fork, not the model.** Ingress cannot set the exec's timeout.
- **Sandboxed Starlark has `repr` and no `base64` or `json`**; the payload is base64 by the sender.
- **Sync ingress and the fork endpoint return `exec_stdout` in full.** `EphemeralResult` carries the raw stdout; only the `exec_log` copy is truncated, to 8 KiB head and tail (`EXEC_LOG_OUTPUT_BYTES` in `src/mshkn/services/lifecycle.py`). So a proposal in a reply always reaches curl whole; the audit copy may lose the middle. Audit lines print first, and the plan decides whether to raise the constant or record proposals by hash in the audit lines.
- **`exclusive` on a fork takes `error_on_conflict` or `defer_on_conflict`**; the brain uses the first, verb chains the second. Today the check (`get_active_computer_for_label`) and the fork are separate awaits with no lock, and the ingress fork-by-label resolves the head in a separate step too; #89 puts both under one per-label lock.
- **Retention is per account, not per label**: `list_prunable_checkpoints` keeps the `checkpoint_retention_count` newest unpinned checkpoints (20 by default) and pinned ones; a quiet chain's head would be pruned. #93 keeps every label's head.
- **The account has one credential**, `accounts.api_key`, resolved by `require_account` in `src/mshkn/api/deps.py`; there is no key table and no record of which credential created a computer. #88 adds both.
- **A VM reaches `api.mshkn.dev` on its own host.** From a bare computer on the live host: `getent` resolves the name to the host's public address, a TCP connect to 443 succeeds, and `curl https://api.mshkn.dev/health` returns 200 over the VM's NAT egress. The bare base has `curl`. Nothing to build.
- **mem0 supports everything §7 needs.** `Memory.from_config` with `vector_store: qdrant` and `path` plus `on_disk: true` creates a local store (files under the path, no server); `llm: anthropic` is a built-in provider; `embedder: openai` is built in. `add()` takes `metadata` and `search()` and `get_all()` take `filters`, which is how provenance is attached and filtered. `add(infer=False)` stores text without an LLM call.
- **Two routes to a scripted embedder with no third-party key.** `embedder: fastembed` runs a local ONNX model (default `thenlper/gte-large`; pulls `onnxruntime`, `tokenizers`, `huggingface-hub`, and downloads the model on first use, so the recipe must fetch it at build time). Or `EmbedderFactory.provider_to_class` is a plain dict, so the membrane can register a deterministic hash embedder for scripted mode (`EmbeddingBase` is two methods, `embed` and `embed_batch`). The plan takes the second: lighter, deterministic, and scripted mode then makes no external call at all when combined with `infer=False`.
- **mem0 phones home by default.** `MEM0_TELEMETRY` defaults to `"True"` and instantiating `Memory` creates a PostHog client. The brain recipe sets `MEM0_TELEMETRY=False`; the invariant that the brain reaches nothing but the two model services depends on it.
- **The brain runs in 1 GB.** Resident memory in the scratch venv: 77 MB after importing the two SDKs, 134 MB after importing mem0, 144 MB with a `Memory` instantiated on an on-disk qdrant store. Site-packages are 135 MB on disk (`qdrant-client`, `numpy`, `sqlalchemy` come with mem0). The scratch measurement was right about the membrane and wrong about the margin: measured on a real brain at the end of the liturgy (#116), the guest reports ~303 MB already in use at rest against 482 MB total, so 512 MB left about 44 MB for the conversation, the relay's stored response and mem0's extraction, and a wake-up fork died there mid-turn. 1 GB and 2 cores stand.
