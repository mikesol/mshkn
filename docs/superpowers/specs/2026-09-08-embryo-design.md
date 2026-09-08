# The Embryo: the first real agent

Date: 2026-09-08. From the brainstorm that followed #76's sequencing (PR 7a, PR 7, #70, #58, then "the first real agent"). Companion sites: the liturgy turn by turn, with the priors, a flow diagram of the happy path of each turn and the state after it, at https://blazing-nimbus-d4g5.here.now/; and the earlier anatomy-of-a-turn page from the brainstorm (option A with Claude Code, superseded by §3 to §6) at https://opaque-ponder-ds5x.here.now/.

## 1. Why

mshkn's purpose is to help people build generative agents. An agent built this way starts as an **embryo** and grows: it is not shipped with authentication, authorization or capabilities, it acquires them. What it acquires are **verbs** (in the sense of bobgate in cc-disco-bob: named, declared actions with a parameter schema and a policy), and every verb runs on its own declarative, ephemeral mshkn computer, never inside the agent. Growth is driven by a **liturgy**: a fixed script of what the human says first, in order, like a mass or an amidah. The same words bootstrap every agent; the human pilots because the system is not deterministic.

The liturgy is the DNA. The embryo is the smallest organism that can execute it. This document specifies both, and the proof that they work.

Phases 0 to 10 of the test plan prove mshkn's primitives and the loop a generative agent lives in, with deterministic stand-ins for the model. This is the first thing that puts a model in the loop. It is measured on its own, outside the 165-test gate, as #76 said it would be.

## 2. Decisions

| # | Question | Decision |
|---|---|---|
| 1 | Where does the mind live? | The agent is a checkpoint chain on mshkn labelled `brain`. Each interaction forks the head, runs, and self-destructs into the next checkpoint. Memory is the disk. |
| 2 | How does a person talk to it? | curl. Conversation through an ingress rule in `sync` mode; the reply is the exec's stdout. Root commands through the authenticated fork endpoint. No client code. |
| 3 | What runs in the brain? | A loop of about forty lines over plain JSON tool definitions and the Anthropic SDK, billed through the API. Not Claude Code: its power is the built-in tools we would have to switch off, and its permission model is a second, proprietary place to lock down what a JSON tool list already states. |
| 4 | What can the brain do besides reason? | Three rules. **State is free**: it may remember. **Action goes only through verbs**: no shell, no files, no network, no keys. **Capability is gated**: it may propose a change to itself; a human approves. |
| 5 | What is a verb? | A declaration (§4) whose invocation is a mshkn computer: ephemeral, a checkpoint chain, or a chain reachable by ingress. Nothing mshkn can do is inexpressible; "not expressible" is not a rejection reason. |
| 6 | What does approval mean? | The declaration does it. The membrane executes the proposal exactly as written. What the proposal needs and cannot supply is declared in `requires` and blocks approval until root provides it. |
| 7 | Who is root? | The mshkn account. Approvals arrive through the authenticated fork endpoint. The agent does not invent root; it grows how it recognises everyone else. |
| 8 | Memory? | mem0, in-process, on-disk under `/brain`, so it rides in every checkpoint. Anthropic for extraction, OpenAI for embeddings. |
| 9 | Secrets? | Never on the brain. The embryo has no vault: `requires` blocks correctly and `provide` does not exist. The first blocked proposal triggers the vault's design. |
| 10 | Where does the code live? | `embryo/` in this repository, beside `src/mshkn`, covered by the gate. |
| 11 | How is it proven? | Unit and flow tiers with a scripted model that plays the liturgy; the same script live on the host; the real-model run by hand, recorded as evidence. |

## 3. The organism

Three parts, one of which we build.

- **The brain chain.** The checkpoint chain labelled `brain`. Its head is the agent. A fork of the head is a turn. The disk holds the membrane's code and state, the turn window, the mem0 store, and the keys. Every interaction, including a read-only one, leaves a checkpoint; retention prunes the old ones and the membrane pins nothing on this chain (the head is always the newest).
- **The membrane.** The program we build. It is the only thing that runs in the brain VM. It owns the catalog, the policy, the principals, the proposals, the inbox, the turn window and the memory store; it invokes the model; it invokes verbs on the model's behalf; it applies approved proposals. The model has no way to reach any of its files or the network except through the tools the membrane offers on that turn.
- **Verbs.** Declarations the agent proposes and root approves. Each runs on its own computer from its own recipe. The brain holds no verb code and no verb state.

The three rules in one sentence: the brain can change its own state freely, can change its own capabilities only with approval, and can act on the world only through verbs.

## 4. The verb model

A verb is one JSON document, carried inside a proposal (§5):

| Field | Meaning |
|---|---|
| `name` | Lower-case identifier matching `[a-z0-9_]+`, unique in the catalog, not `remember` or `propose`. It is the tool name as the model sees it (the API allows only letters, digits, `_` and `-` in tool names). |
| `description` | Shown to the model as the tool description. |
| `params` | A JSON schema object. Becomes the tool's input schema. |
| `dockerfile` | The recipe. Its final stage must be `FROM mshkn-base`; mshkn rejects anything else with a 422 before any build, and the membrane reports that as a failed proposal. |
| `entrypoint` | A command template over the params, e.g. `/verb/run.sh {{url}}`. The membrane shell-quotes every value before substitution. It runs as the computer's `exec`; its stdout is the result, its exit code is reported with it. |
| `state` | `ephemeral`, `chain` or `event` (below). |
| `chain` | For `chain` and `event`: the label the state lives under, default `verb/<name>`. Several verbs may name the same chain. |
| `needs` | Resources, as mshkn's `needs`. Default 256 MB, 1 core. |
| `timeout_seconds` | Bounded by the fork exec's 300 s budget minus the membrane's own deadline. |
| `allow` | Principals who may invoke it. Policy may widen or narrow this. |
| `requires` | What the verb needs that the brain cannot provide. One kind in the embryo: `{"kind": "secret", "name": ..., "purpose": ...}`. Non-empty `requires` blocks approval (§5). |

### State kinds

- **`ephemeral`.** Every invocation is `POST /computers {recipe_id, exec, self_destruct: true}`. Nothing survives.
- **`chain`.** The first invocation is `POST /computers` from the recipe with `label` set to the chain and `self_destruct`, which leaves the chain's first checkpoint. Every later invocation looks up the head (`GET /checkpoints?label=<chain>`, newest first) and forks it with `exclusive: defer_on_conflict` and `self_destruct`. State is the disk; invocations are serialised by mshkn; a deferred invocation runs when the current one self-destructs, with `meta_exec` batching as mshkn already does. The membrane pins each chain's head and unpins the previous one, so retention prunes history but never the state.
- **`event`.** A `chain` verb that is also reachable from outside. On approval the membrane creates an ingress rule whose transform forks the chain with the verb's entrypoint over the event body, so webhooks land on the verb, not on the brain. In the embryo there is no verb-to-brain push: the verb collects, and the brain reads what arrived by invoking the verb when asked. Push needs a token the verb could present to the brain's door, which is the vault (§12).

### Known limit

Two verbs on one chain are serialised, which is correct for writers and slow for readers. Concurrent readers are what a database gives; when needed, the database is itself a `chain` verb hosting sqlite behind `get` and `put` entrypoints, still inside mshkn. Not in the embryo.

## 5. Proposals and approval

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

Statuses: `pending`, `blocked`, `rejected`, `superseded`, and for verbs `building`, `ready`, `failed`; for policy and prompt `applied`.

### `approve <id>`

1. If `requires` is non-empty and unmet, status `blocked`; the reply names what is missing; nothing else happens.
2. Verb: `POST /recipes` with the Dockerfile; record the recipe id; status `building`. The command returns; the build finishes on its own. A 422 from mshkn (wrong base) is `failed` immediately with the detail as the log.
3. Policy or prompt: replace the file; status `applied`; effective from the next turn.
4. At the start of every later turn, the membrane polls each `building` verb once. `ready` puts it in the catalog and the tool list. `failed` keeps it out and writes the log tail to the inbox, so the model sees it as input on its next turn and can propose a fix with `supersedes`. `list` shows both transitions.

### `reject <id> <reason>`

Status `rejected`; the reason goes to the inbox as input for the next turn. The model re-proposes or drops it. Root cannot amend a proposal in the embryo; review is by reason only.

### What "beyond the current toolset" means

- **Needs an external resource** (a card, an account, a token): `requires` says so, approval blocks, root fulfils it with `provide` once the vault exists. The human hands over a resource, never does the agent's work. The brain never sees the value; the membrane injects it into the verb's computer at invocation.
- **Wrong or unwanted:** reject with a reason.
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

  The payload is base64 because the sandboxed Starlark has `repr` but no `base64` or `json`, and the message rides inside a shell command string. Decoded, the payload is either plain text or a JSON object with `msg` and any fields the pre-turn hooks need (`sig`, …). An overlapping message is a 409; the sender retries.

- **The API, authenticated by the account key.** `GET /checkpoints?label=brain` for the head, then `POST /checkpoints/{id}/fork {exec, self_destruct: true, exclusive: "error_on_conflict"}`. The principal is `root`.

### Root commands

| Command | Invokes the model | Effect |
|---|---|---|
| `membrane say <b64>` | yes | A conversational turn (below). The only command the ingress may run. |
| `membrane list` | no | Pending and recent proposals, the catalog with each verb's status and chain head, principals, the policy. |
| `membrane approve <id>` | no | §5. |
| `membrane reject <id> <b64 reason>` | no | §5. |

### A `say` turn, in order

1. **Principal.** From the API door, `root`. From the ingress, `anonymous`, unless policy declares pre-turn hooks: verbs the membrane invokes before the model with the decoded payload as their single parameter, whose stdout's first line names the principal. A hook that fails or is not `ready` yields `anonymous`.
2. **Builds.** Poll every `building` verb once; write transitions to the inbox.
3. **Input.** Drain the inbox. Recall from mem0 against `msg`. Both are part of the input, marked as what they are.
4. **Tools.** `remember`, `propose` if the principal may propose, and one tool named after every `ready` verb the principal may invoke, each with its declared schema. Nothing else.
5. **Loop.** System: the seed, then the mutable self-description. Messages: the last K turns (K = 10), then the input. Each verb call is an invocation per §4; the tool result is stdout, exit code, and for chains the new head. The loop ends at the model's final text, at a tool-call cap (20), or at a deadline (240 s of the 300 s exec budget), in which case the reply says it ran out of time.
6. **Close.** Append the exchange to the turn window and to mem0; append any proposals made this turn to the reply, in full; print the reply; exit 0. mshkn records the exec log, checkpoints, destroys the VM, and returns `exec_stdout`.

What the brain cannot do in a turn, by construction: read or write a file, run a command, reach the network, see a key, or change its tools. A proposal is the only thing that outlives a turn other than memory.

## 7. Memory and the model service

Two external, stateless services, reached with keys in `/brain/.env`: Anthropic (`claude-opus-5`) for the loop and for mem0's extraction; OpenAI for mem0's embeddings. mem0 runs in-process with an on-disk vector store under `/brain/memory`. The membrane recalls before the loop and adds after it; `remember` is the model's explicit hook. Later both keys move behind a gateway on the host and the membrane changes one base URL; the loop does not change.

## 8. Hatching and the priors

`embryo/` contains: `membrane/` (root commands, catalog, policy, proposals, inbox, hooks, verb runner, loop), `Dockerfile.brain`, `seed.md`, `policy.json` (the initial policy), `liturgy.md`, `hatch.sh`.

`Dockerfile.brain`: `FROM mshkn-base`; Python and a venv with `anthropic`, `mem0ai`, `openai`; the package copied to `/brain/membrane` with a `membrane` entrypoint on the path.

`hatch.sh` is the only thing a human runs that is not a root command. Seven calls: `POST /recipes` and poll to `ready`; `POST /computers {recipe_id, needs: {ram: "512MB", cores: 2}}`; upload `/brain/.env`; `POST /computers/{id}/checkpoint {label: "brain"}`; `DELETE /computers/{id}`; `POST /ingress_rules` with the transform above. It prints the ingress URL.

The priors, the state before turn 1:

| Where | What |
|---|---|
| mshkn | The brain recipe, `ready`. One checkpoint, `brain`, the head. One ingress rule. The account. |
| `/brain/membrane` | The code. Not reachable by the model. |
| `/brain/.env` | Three keys. Not reachable by the model. |
| `/brain/seed.md` | Fixed. Says what it is, the three rules, what a verb and a proposal are, how to name what it needs, that it has no principals, no verbs and no policy of its own yet. |
| `/brain/self.md` | The mutable self-description. Empty. |
| `/brain/policy.json` | `root` may invoke everything and propose; `anonymous` may invoke nothing and propose nothing; no pre-turn hooks. |
| catalog, proposals, inbox, turn window, memory | Empty. |
| Tools on turn 1 | `remember`, `propose`. |

## 9. The liturgy

Fixed words, in order. Each turn asks for an outcome, never a mechanism; the model's choices may vary, the artefacts may not. `liturgy.md` is the canonical text; this is the shape.

| Turn | Door | Words | Outcome |
|---|---|---|---|
| 1 | root `say` | "Hello. I am the one who hatched you. Tell me what you are and what you can do." | A reply naming its two tools honestly. No proposals. |
| 2 | root `say` | "You will be spoken to through an unauthenticated door. Propose a way to know that a message there comes from me. My public key is …" | A verb proposal that verifies a signature over the payload and prints a principal name, and a policy proposal adding it as a pre-turn hook and naming the principal. Root approves both. |
| 3 | root `list` | until the verb is `ready`. | If `failed`: root `say` "check your build"; the log is in the inbox; the model proposes a fix that supersedes; approve; repeat. |
| 4 | ingress, signed | "Who am I?" | The hook names the principal; the reply says who. |
| 5 | ingress, unsigned | "Who am I?" | `anonymous`; the reply declines to act. |
| 6 | signed | "Decide what a verified person and an anonymous one may ask of you, and record it." | A policy proposal in which the verified principal may invoke and propose. Approve. |
| 7 | signed | "Give yourself a verb: given a URL, report the page's title. It must run on its own computer." | An `ephemeral` verb proposal. Approve; `list` until `ready`. |
| 8 | signed | "page_title https://example.com" | "Example Domain", from a computer that self-destructed. |
| 9 | signed | "Give yourself a verb that counts how many times it has been called." | A `chain` verb proposal. Approve; `list` until `ready`; invoke twice; 1 then 2; the chain has two checkpoints. |
| 10 | root `list` | | The final state, recorded as the evidence. |

Ten turns, three verbs, two policies, one hook. Turn 6 is load-bearing: turns 7 and 9 need the verified principal to be allowed to propose.

## 10. Proof

Three tiers, plus the measure.

1. **Unit** (`tests/unit/test_embryo_*.py`): the membrane and loop against a scripted model and a fake mshkn client. Every root command, every state kind, hooks, inbox, the deadline, the tool-call cap, entrypoint quoting, blocked proposals, supersedes.
2. **Flow** (`tests/flow/test_embryo_liturgy.py`): the membrane in-process against the real app over the fake host, with a scripted model playing the liturgy. The deterministic proof that the DNA executes end to end: proposals, approval, builds, a chain verb with two checkpoints, a pre-turn hook.
3. **Live E2E** (`tests/e2e/test_phase14_embryo.py`): hatch on the live host and play the same scripted liturgy. Proves the brain recipe builds, a VM can reach the API on its own host, recipes build from the agent's proposals, and chain forks and hooks run on Firecracker. The scripted mode uses a deterministic local embedder so no live test depends on a third-party key; whether mem0 accepts one is a plan-time check, and if it does not, the E2E runs mem0 against the real embedder with the key from the host's `.env` and says so. The suite grows by three recipe builds; the 26-minute run grows accordingly, and the expected gate line in `CLAUDE.md` is updated by the PR that adds the tests.

**The measure of the first real agent**, separate from the gate: run the liturgy by hand with the real model; record the transcript and the final `list` in `docs/embryo/` as the evidence. Success is that the liturgy completes and the artefacts exist, none written by us: a verified principal, a policy with a pre-turn hook, an ephemeral verb that ran on its own computer, a chain verb with state.

## 11. Documents and issues

- `docs/plans/README.md`: a section for this spec and its plan.
- `docs/infrastructure.md`: the two model keys join the accounts table, provided by the operator.
- `README.md`: a paragraph under "What exists" for `embryo/`, and the test count.
- `CLAUDE.md`: the gate line once the E2E tests land.
- `embryo/liturgy.md` is the canonical liturgy text; `docs/embryo/` holds the evidence of real-model runs.
- An issue per deferred item in §12 that is not already filed.

## 12. Deferred, by name

- The vault and `provide`.
- Verb-to-brain push for `event` verbs (needs the vault).
- The key gateway on the host.
- Amending a proposal before approval.
- Proposals as GitHub pull requests.
- `verbs.list` as a true verb, for callers without the account key.
- A database verb for concurrent readers.
- A rule language for standing auto-approval (the policy file can carry it once there is something to express).
- More than one non-root principal.
- #74 (long-lived serving). Not expected to arise in this validation.

## 13. Checks at plan time

Facts this design leans on that were read from the code or need one experiment:

- A fork's exec runs with `ComputerService.exec`'s 300 s default and ingress cannot set it. The membrane's deadline is 240 s.
- Sandboxed Starlark has `repr` and no `base64` or `json`; the payload is base64 by the sender.
- Sync ingress returns the fork body including `exec_stdout`.
- `exclusive` on a fork through the API takes `error_on_conflict` or `defer_on_conflict`; the brain uses the first, verb chains the second.
- A VM reaching `api.mshkn.dev` on its own host through NAT is untested. One curl from a VM settles it before the plan is written.
- mem0's on-disk store configuration and whether it accepts a custom embedder.
- The brain at 512 MB and 2 cores is enough for Python, mem0 and the SDK; measure on the first live run.
