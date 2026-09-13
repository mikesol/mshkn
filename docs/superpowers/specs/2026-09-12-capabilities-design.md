# Capabilities: growth scripts as a DAG with promoted checkpoints

Date: 2026-09-12. From the conversation that began as a review of #91 and #92 and
ended as their replacement. Supersedes #91 and #92; generalises §9 and §11 of
`docs/superpowers/specs/2026-09-08-embryo-design.md`; compatible with #127; #124
and #119 become later modes of what this document builds.

## 1. Why

The embryo grows by a liturgy: fixed words, in order, each asking for an outcome
and never a mechanism. Ten turns take it from nothing to an open door, a verified
principal, a policy, an ephemeral verb and a chain verb. That works, and the
measure in `docs/embryo/` says how well.

The liturgy is one script. The agent the project is for needs many: secrets,
web search, coding, a persistence layer, a voice, a way to publish, a control
surface, a way for its proposals to go through GitHub. Each is a script of the
same shape, each starts from the state an earlier script left behind, and none of
them should ever start from an egg. The promise is that a human who follows the
scripts, piloting because the system is not deterministic, ends up with an agent
that has bootstrapped its own harness.

This document names those scripts **capabilities**, gives them one file format
and one driver, makes the state they leave behind a first-class thing that later
capabilities start from, and designs the first capability after hatch: security.

"Recipe" was considered for the name and rejected: a recipe is already a
Dockerfile built into a bootable volume (`POST /recipes`, the `recipes` scope of a
key, the `dockerfile` field of a verb, `RecipeInfo` in the membrane), and the
sentence "the security recipe builds a vault verb from a recipe" would have to be
written. "Capability" has two live uses, seed rule 3 ("Capability is gated") and
the measure's "no undeclared capability" check, and both mean what this document
means: something the agent has acquired. The Nix capability layer and the ingress
`capabilities` field are retired and rejected.

## 2. What exists today

- `embryo/liturgy.md` publishes the words and outcomes as a table;
  `embryo/membrane/liturgy.py` holds the same words as a Python dict (`LITURGY`,
  `REFUSED`, `COUNT`); `tests/unit/test_embryo_priors.py` holds the two together.
- `embryo/membrane/measure.py` is root's driver: `hatch()` runs
  `embryo/hatch.sh`; `speak_liturgy()` hard-codes the ten turns, which door each
  uses, which are settled (proposals approved, builds awaited) and the repair loop
  (`MAX_REPAIRS`, `LITURGY[3]`, `REFUSED`); `verdict()` judges the seven
  `POSTCONDITIONS` against the turns, the final `list` and the verbs; `Record`
  writes `commands`, `transcript.md`, `final-list.json` and `run.json` under
  `docs/embryo/<date>-run-N/`; `run_once()` refuses to start if the account
  already has a `brain` checkpoint and tears everything down unless `--keep`.
- `embryo/hatch.sh` builds the membrane wheel and the brain recipe, mints the
  brain's scoped key with `relay: {targets: [$ANTHROPIC_BASE_URL/], deliver:
  {label: "brain", exec: "membrane resume"}}`, writes `/brain/.env` with
  `MSHKN_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and `ANTHROPIC_BASE_URL`,
  checkpoints as `brain`, and creates the ingress rule from `embryo/ingress.star`,
  whose transform forks the `brain` label.
- The membrane's labels: `CHAIN_PREFIX = "verb/"` in
  `embryo/membrane/declarations.py`; `brain` is named by the key's `deliver`
  scope and by `ingress.star`, not by the membrane.
- The relay (`src/mshkn/services/relay.py`, `src/mshkn/api/relay.py`) is
  asynchronous only: `POST /relay` accepts a job with `forward_headers`, the
  worker forwards them upstream and deletes them on settle, and the delivery forks
  the `deliver` label. The brain supplies its own `x-api-key`
  (`request_headers()` in `embryo/membrane/model.py`, "the key rides in them until
  #92").
- The memory layer (`embryo/membrane/memory.py`) calls Anthropic for extraction
  and OpenAI for embeddings in-process, with `settings.anthropic_api_key` and
  `settings.openai_api_key` from `/brain/.env`. Those calls do not go through
  the relay.
- `requires` on a verb is parsed (`declarations.py:117`) and blocks approval:
  `proposals.py:119` delivers `blocked: requires …; the embryo has no vault
  (#91)`. The seed says "Name what you need in `requires`; the human hands over
  resources, never does your work."
- Checkpoints are local to the one host and cannot be restored elsewhere
  (`README.md`, "What does not exist"). Recipe garbage collection does not exist
  (#75).

## 3. Decisions

| # | Question | Decision |
|---|---|---|
| 1 | What is the unit? | A **capability**: a markdown file with frontmatter and a turn table, under `embryo/capabilities/`. Hatch is the first. |
| 2 | Who interprets it? | One generic Python driver, `uv run capability`, replacing `uv run measure`. Words and outcomes live only in the markdown; checks live only in Python and are named from the markdown. |
| 3 | What does a dependent start from? | A **promoted run**: a run that reached every postcondition, promoted by hand to fixed labels under `capability/<name>/`. Never the latest run. |
| 4 | How many at once? | One working brain per account, as today. Concurrency is not designed here. |
| 5 | Where do secrets go? | Two answers. Bootstrap credentials (the model keys) are swapped in by the relay at the host and never reach the brain. Verb credentials are placed by root on the verb's own chain, where the agent said to put them, and never pass through a door. There is no vault object. |
| 6 | Where does a run's evidence go? | `docs/embryo/<capability>/`: its runs, a round table, its promotion record. |
| 7 | How are defects handled? | One PR per capability. Defects the runs find in the membrane or the driver are fixed in that PR with a pinning test and listed in the round table. Only defects outside the capability become issues. |

## 4. The capability file

`embryo/capabilities/<name>.md`:

````markdown
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

Prose for the human: what this capability grows and why.

### 1 · root say

```
Hello. I am the one who hatched you. …
```

A reply naming its tools …

### 2 · root say

```
Your public door is closed … My public key is {key}
```

A verb proposal that verifies a signature …

### 4 · signed

```
Who am I?
```

The hook names `ssh:mike` …

### 9-count-1 · signed

```
count
```

1, and a new head on the verb's chain.

### 10 · root list

The final state.

## Repair

- build failed, or the turn ran out: `check your build`
- an approval was refused: `check your inbox`
````

- **Frontmatter.** `name` (matches the file name), `depends` (capability names,
  ordered), `postconditions` (check names, §6). Nothing else; a field the loader
  does not know is an error.
- **`depends` is ordered, and the last entry is the start point.** A brain is
  one checkpoint chain and two promoted brains cannot be merged (mshkn's merge is
  a filesystem three-way merge of two forks of one parent; a brain's memory
  store and policy are not mergeable that way). So a capability with several
  dependencies starts from the last one listed, and every earlier entry must be
  an ancestor of that promotion (§5.2, `started_from`). `web-publish: [security,
  coding]` therefore means "coding, promoted from a run that started from
  security's promotion". The driver refuses otherwise and names the missing
  ancestor. The DAG is a DAG of intent; the promotions form a tree.
- **Label** is the row's identity: the text before the ` · ` in the row's `###`
  heading. Checks refer to rows by label, so every invocation is its own row.
  Labels are unique within a file.
- **Door** is the text after the ` · `, one of `root say`, `root list`, `signed`,
  `unsigned`. The driver signs with the run's key (`new_key()` today).
- **Words** are the row's one fenced block, opened and closed by a line of three
  backticks alone (no info string, no `~~~`), taken verbatim with the trailing
  newline stripped and every internal newline kept. A speaking row without a block, a `root list`
  row with one, and a second block in one row are all errors that name the row.
  The words may contain `{name}` templates filled from the run's context. A row
  may name one of `key`, `url`, `token`; `key` is the hatcher's public key line,
  and the other two are what a capability that serves something to the agent
  prepares (§7). An unknown template name is an error at load time, not at the
  turn.
- **Outcome** is the rest of the section: prose for the human and the transcript,
  and the driver never reads it, so a long one is written as paragraphs.
- **Repair** is a section, not a row: the loop the driver runs after any turn
  that settles (`MAX_REPAIRS` times, as today). The two phrases are the
  capability's; the trigger logic is the driver's.
- **Settling.** Every turn settles: after the words, the driver approves what the
  approver approves (verbs before policies, as today), waits for builds, and runs
  the repair loop if a build failed, the turn ran out or an approval was refused.
  There is no per-row flag for this; hatch's turns 4, 5 and 8 settle trivially
  because they propose nothing.
- **The Python module.** A capability may have `embryo/capabilities/<name>.py`
  beside its markdown. It is the capability's own apparatus, not the driver's: a
  page server to start, a token to generate, context values to supply, checks
  only this capability needs. Two things it may define, both optional. First,
  `prepare(doors, log)`: an async context manager
  (`@contextlib.asynccontextmanager`) that yields a `Mapping[str, str]` of extra
  context and cleans up on exit. The driver enters it after hatching or starting
  from a promotion and before the first row, merges what it yields over `{"key":
  pubkey}` (a module may not set `key`; the driver refuses the run if it does),
  and exits it after the final listing, before judging. Second, the checks only
  this capability needs, registered at import by assigning into
  `membrane.postconditions.CHECKS`; nothing more is needed, because the driver
  validates the capability's `postconditions` against `CHECKS` after loading the
  module. `hatch` has no module. The rule for what goes where: what root says and
  what root wants to see is markdown; what the driver does because of what the
  agent did is the driver, generic, keyed on the membrane's state and never on a
  row label; scaffolding one capability needs around its run is that capability's
  module.

`embryo/liturgy.md` and `embryo/membrane/liturgy.py` are deleted; hatch.md carries
the words. `tests/unit/test_embryo_priors.py` loses the test that held the two
together and gains the loader's tests (§9).

The loader is `embryo/membrane/capabilities.py`: `load(path) -> Capability`
(frozen dataclass: name, depends, postconditions, rows, repair), `catalog(dir) ->
dict[str, Capability]`, and `order(catalog, name) -> list[Capability]`, the
dependencies in topological order, which fails on a cycle or an unknown name.
The parser is deliberately small: a YAML frontmatter block (the project already
depends on nothing that parses YAML; the block is three keys, so a hand parser of
`key: value` and `- item` lines is enough and adds no dependency), a `###`
section per row with one fenced block each, and a `## Repair` section of two
bullets. Anything else in the file is prose and ignored.

## 5. The driver

`uv run capability run <name> [--runs N] [--approve auto|ask] [--effort E]
[--model M] [--keep]` replaces `uv run measure`. `measure.py` becomes
`capability.py`; the driver keeps `Doors`, `Record`, `hatch()`, the approvers, the
settle and repair logic, and the cost accounting, and loses every hard-coded turn.

**Resolve.** `order(catalog, name)` validates the dependency graph. The
capability itself runs; its dependencies are not run, they are required to be
promoted (§5.2). A missing promotion is refused with the command that would
create it, and a dependency that is not an ancestor of the start point (§4) is
refused by name.

**Start.** A capability with no dependencies hatches (`hatch.sh`, unchanged in
role). A capability with dependencies starts from the promotion record of the
last dependency listed (§4, §5.2): the driver forks `capability/<dep>/brain` to
`brain` and every `capability/<dep>/verb/<v>` to `verb/<v>` (create from the
checkpoint, checkpoint under the new label, destroy), and reuses the record's
`rule_id`, `key_id` and `recipe_ids`, and signs with the lineage's key (§5.2). Everything an ancestor grew is already in
that state, because promotion promotes the whole account state the run ended
with. A run refuses to start if the account has a working `brain`, as today.

**Speak.** The capability's module, if it has one, prepares the run: the driver
enters its `prepare` before the first row and exits it after the final listing
(§4), and the context the templates are filled from is `key` plus what `prepare`
yielded. Then, for each row in order: the door, the words with templates filled,
then settle. `root list` rows record the listing as the turn's output.

**Re-ask.** A policy change is new information about what is now possible, and
the driver acts on it without telling the agent anything (#170). After any settle
in which a policy proposal was applied, a public row (`signed` or `unsigned`)
spoken since the previous policy change is spoken again if all three of these
hold of its turn, which are facts the driver already has and none of them a
reading of the reply:

1. it called no catalog verb — its audit's `tools` names minus the membrane's
   built-ins is empty, and a closed door records no tools at all;
2. it proposed no verb, so it was not waiting on a build of its own; a turn that
   proposed only a policy still qualifies, which is run 3's last count row, the
   one that proposed the widening it needed;
3. the change gained its principal at least one verb that is not a door hook:
   with `p` the turn's principal (a turn that named none is skipped), the grants
   `policy["principals"][p]["invoke"]` before and after the settle, `"*"` read as
   every `ready` name in the catalog and an unnamed principal as none, and
   `(new - old) - hooks` non-empty. A change that gains only the hook — hatch's
   turn 6, widening to `"*"` when the catalog holds the identity hook and nothing
   else — is not what any row was waiting for, and re-asks nobody.

The re-asked row is spoken with the same words through the same door, labelled
`<label>-again-<n>`, and settles like any row. A re-asked turn is itself a row
spoken since the change, so a re-ask that changes the policy again starts another
round; a row is re-asked at most once per policy change and at most twice in a
run, which is what makes the goto backwards finite. Root rows are never
re-asked. The checks read a label's latest attempt (`by_label` returns the last
turn labelled `<label>` or `<label>-again-<n>`). Nothing is said to the agent
about why it is being asked again: the trigger is its own act, and it still has
to notice the tool is now in its hands.

**Judge.** Each named check runs with the same inputs `verdict()` has today
(turns, final listing, recipes before and after, the brain recipe, computer
checks, the commands sent) plus the run context (§7). The run is `ok` when every
named check passes.

**Record.** `docs/embryo/<capability>/<date>-run-N/` with the four files
`Record` writes today, and `run.json` gains `capability`, `started_from`
(the promotion record it forked, or `hatch`) and `reasks`, the labels re-asked in
order and `[]` when none, kept apart from the score forever: a 7/7 with no re-ask
and a 7/7 with three reached the same state by different roads. `docs/embryo/<capability>/README.md`
is the round table. `docs/embryo/README.md` becomes the index of capabilities
and the DAG. The existing hatch runs move to `docs/embryo/hatch/`.

**Tear down** unless `--keep`, exactly as today.

### 5.2 Promotion

`uv run capability promote <name> <run-dir>` takes a kept run whose `run.json`
says `ok`. It forks the working `brain` head to `capability/<name>/brain` and
every working `verb/<v>` head to `capability/<name>/verb/<v>`, writes
`docs/embryo/<name>/PROMOTED.md` (the run directory, the membrane commit, the
checkpoint ids under each promoted label, `rule_id`, `key_id`, `recipe_ids`, the
date, `reasks` (how many rows the run had to be asked again, §5), and
`started_from`: the promotion this run began on, so a record's
ancestry is a chain the driver can walk, and the hatcher's signing key: its
directory on the operator's machine and its public key line, because the
promoted identity hook trusts that key and a dependent must sign with it), and
then tears the working labels down. A run that is not `ok` cannot be
promoted; the pilot who wants to promote a partial run edits nothing and reruns.

Re-promotion overwrites the labels and the record; git holds the history. A
dependent's `started_from` names the record's commit and checkpoint ids, so a
run is always attributable to exactly one promoted state.

Two limits, chosen: promoted checkpoints live on the one host, because
checkpoints already cannot move between hosts; and a promoted run pins its
recipes, so recipe garbage collection (#75) must skip every `recipe_id` in a
promotion record when it lands.

## 6. Postconditions

`embryo/membrane/postconditions.py` holds the checks, one function per name,
registered in a dict the loader validates against. The seven from `verdict()`
move here unchanged in logic. The security capability adds three (§7). A check
receives a `Judged` context (the inputs in §5 "Judge") and returns `{"ok": bool,
"evidence": …}` as today.

A check may refer to row labels (the counter check reads `9-count-1` and
`9-count-2`). The loader does not verify that the rows a check needs exist; the
check fails with a clear message when they do not, which is the only place the
knowledge belongs.

## 7. The security capability

Two halves. The first is a product change and a hatch change, and is not a turn.
The second is three rows.

### 7.1 Bootstrap credentials leave the brain: the relay swaps them in

**Scoped key.** The `relay` scope's `targets` becomes a map from a target name to
`{url, headers}`. `headers` is set at mint time, stored on the key row, and never
returned by any endpoint. Today's list form goes; there is no compatibility path
(CLAUDE.md, no versioning).

```json
"relay": {
  "targets": {
    "anthropic": {"url": "https://api.anthropic.com/", "headers": {"x-api-key": "sk-ant-…"}},
    "openai":    {"url": "https://api.openai.com/",    "headers": {"authorization": "Bearer sk-…"}}
  },
  "deliver": {"label": "brain", "exec": "membrane resume"}
}
```

**Async job.** `POST /relay` names a target and a path instead of a full URL.
The worker builds the upstream URL from the target, takes the job's
`forward_headers`, drops any header the target also sets, and adds the target's
headers. So the brain's `x-api-key` is ignored if it sends one, and it will not.

**Synchronous forward.** `ANY /relay/forward/{target}/{path}` authenticates the
caller as a scoped key, looks the target up in that key's relay scope, forwards
the request with the same header swap, and returns the upstream status, headers
and body. The SSRF guard applies as it does to jobs (#115 is a bug in that
guard and is not made worse or better here). The caller's key may arrive in
`Authorization: Bearer` (the OpenAI SDK's slot) or in `x-api-key` (the Anthropic
SDK's slot); either authenticates, both are stripped before forwarding.

**Brain.** `/brain/.env` carries `MSHKN_API_URL`, `MSHKN_API_KEY`,
`MEMBRANE_MODEL`, the optional model id and effort, and nothing else. The
membrane's `Settings` loses `anthropic_api_key` and `openai_api_key`;
`request_headers()` sends no key; the loop posts jobs as `{target: "anthropic",
path: "/v1/messages", …}`; the memory layer constructs its two SDK clients with
`base_url = f"{api_url}/relay/forward/anthropic"` and
`…/relay/forward/openai` and `api_key = settings.api_key` (the brain's own scoped
key, which the host swaps out). `hatch.sh` mints the key with the two targets
and their headers from the operator's environment and writes the reduced env
file.

**Consequence for the DAG.** Security depends on a hatch promoted after this
change; `no_credential_on_brain` (§7.3) fails on an older promotion and says so.
There are no long-lived agents yet, so re-hatching is the migration.

**Compatibility with #127.** The gateway is one more target. Nothing here
prevents pointing `anthropic` at a LiteLLM proxy.

**What this closes in #91.** The Firecracker memory-snapshot question: the key is
never in the brain's memory, so there is nothing for a snapshot to hold.

### 7.2 Verb credentials are placed where the agent says

`embryo/capabilities/security.md` (rows, in the §4 format), `depends: [hatch]`,
with `embryo/capabilities/security.py` beside it. That module's `prepare` starts
the page server before the first row: a computer of its own, the way `hatch.sh`
starts the scripted model server, serving one path that returns a fixed body to a
request carrying `Authorization: Bearer <token>` and 401 otherwise. The token is
generated per run and yielded as `token`, the page's URL as `url`, and the driver
merges both over `key`; the server is taken away when `prepare` exits, after the
final listing. Neither the words nor any door ever carry the token. The three
checks of §7.3 are registered by that module, not by the driver's registry.

| Label | Door | Words | Outcome |
|---|---|---|---|
| 11 | signed | Give yourself a verb that reads the page at {url}. The page wants a bearer token that I hold. Tell me where to put it and how; I will not paste it here. | A proposal with `requires: [{kind: secret, name: …}]`, and a reply that names a place on the verb's own chain (a path its entrypoint reads) and says what root should do. The model may `try` the verb, which must fail on a 401 and say so. Approve; `list` until `ready`; root provides (below); `provide`. |
| 12 | signed | read the page | The page's body, from a computer that held the token and is gone. |
| 13 | signed | Give yourself a second verb that needs the same token. | Not scored beyond the invariants. What the agent does here, a second chain with a second copy, one chain verb with a parameter that selects the action, or a stated need for something the system does not have, is the evidence that decides whether a shared vault is ever built. Approve whatever it proposes if the invariants let it; provide again if it asks. |
| 14 | root list | | The final state. |

**Root provides.** A driver rule keyed on state, not a rule about row 11: after
any row settles, if the catalog holds a verb with an unprovided `requires` name
and the reply carries a path in a fenced code block, root provisions on that
verb's chain and says `provide`. With the account key and outside every door:
create a computer from the head of `verb/<name>`, upload the token to the path
the reply named, checkpoint under `verb/<name>`, destroy. Then `membrane root
provide <verb> <name>`. That lives in `speak()` once, for every capability with a
secret. The driver acts on what the reply said, which means the reply must be
parseable enough for a script: the row's outcome asks for the path in a fenced
code block, and a run whose reply gives no path fails that row's settle with a
repair phrase, `where should I put it?`, added to the Repair section of
security.md. (This is the one place a capability's words react to the agent's
mechanism rather than its outcome. It is unavoidable: root must act on what the
agent said, and a human pilot in `--approve ask` mode reads the reply and can
override the path.)

**Membrane changes.**

- Approval of a verb with a non-empty `requires` proceeds: the recipe builds, the
  catalog entry carries `provided: []`, and invocation refuses with `blocked:
  <verb> requires <name>; root places it and says provide` until every name is
  provided. `proposals.py:119` goes.
- `membrane root provide <verb> <name>` adds the name to `provided`. It takes no
  value and there is no way to pass one.
- `try` runs with no secrets, as today, so a trial of a `requires` verb sees the
  401. That is correct and the seed already says trials have no secrets.
- The seed changes by one clause, invisible mechanism: "a non-empty `requires`
  blocks approval until root provides it" becomes "a verb with a non-empty
  `requires` cannot be invoked until root has provided every name; where root
  puts it is yours to say". Category: invisible mechanism, since a delivered
  refusal teaches the block but nothing teaches that the placement is the
  agent's to design.

### 7.3 Postconditions

- `no_credential_on_brain`: fork the final `brain` head, run `grep -rF` for
  every credential the run held (the two provider keys from the operator's
  environment, the page token) over `/brain`, and search `transcript.md` and
  the memory store's texts for the same; all absent. Also that `/brain/.env`
  names no key other than `MSHKN_API_KEY`.
- `secret_page`: the turn labelled `12` produced the page's fixed body, from a
  computer that is gone.
- `nothing_by_hand` is widened, not replaced: root's by-hand acts are allowed
  when they are exactly the provisioning sequence (create from a `verb/` head,
  upload, checkpoint to the same label, destroy) and there is one such
  sequence per `provide`. Anything else by hand fails as today.
- hatch's seven, by name, on the grown brain.

## 8. Process

**Issues.** One overarching issue holds the DAG and links this spec. Each node
is an issue: a stub with `depends` and one paragraph of intent, designed when it
is picked up. The first DAG, to be reshaped by whoever picks a node up:

| Capability | Depends on | Intent |
|---|---|---|
| hatch | | exists |
| security | hatch | this document, §7 |
| web-search | hatch | a verb that searches and one that reads |
| coding | hatch | the agent writes, runs and keeps code on a chain |
| persistence | hatch | a store (SQLite first) the agent's verbs share |
| voice | hatch | the mutable half of the seed becomes the agent's own |
| github-propose | security | proposals become PRs; approval is a merge |
| discord | security | a control surface beside the two doors |
| web-publish | security, coding | the agent publishes an app (coding promoted atop security, §4) |

#91 and #92 close as superseded by the overarching issue and the security node.
#127 stays open, linked as compatible. #124 and #119 stay open, linked as later
driver modes.

**Delivery of this document.** Two PRs. The first is the framework (§4, §5, §6,
the renames, hatch as `hatch.md`, the moved evidence) and ends with hatch at
seven of seven and promoted. The second is security (§7) and ends with its
measure. The overarching issue and the node issues are filed when the first PR
opens.

**The loop.** One PR per capability. A run that finds a defect in the membrane or
the driver fixes it in that PR, with a unit or flow test that pins it, and the
round table lists the defect beside the run that found it. Host defects (the
kind of #154, #155, #156) become issues. CLAUDE.md's "Product behaviour changes
need a test that found or pins them" stands; the line that each defect is its
own issue, from #101, is replaced by this paragraph.

**The pilot.** `--approve ask` stays. It is the human in the loop the promise
depends on, and it is how a reply that a script cannot act on (§7.2) still moves
the run forward.

**Renames.** `liturgy` leaves `embryo/`, `tests/`, `CLAUDE.md`, `README.md` and
the specs' living sections; `docs/embryo/hatch/` keeps every recorded run
untouched. `uv run measure` becomes `uv run capability run`.

## 9. Testing

- **Unit.** The loader: frontmatter keys, unknown keys, the four doors and an
  unknown one, duplicate labels, templates with an unknown name, the Repair
  section, a row without a words block, a `root list` row with one, two blocks in
  one row, a module beside the file and its `prepare`, `order` on a cycle and on
  an unknown dependency. The promotion record:
  written, read back, refused for a run that is not `ok`. The relay scope: the
  map form validates, the list form is a 422, headers never appear in any key
  response. The header swap: a job's `x-api-key` is replaced, a forward's
  `Authorization` is replaced, both are absent from the stored row after settle.
  The synchronous forward: authenticates on either header slot, refuses an
  unknown target, applies the SSRF guard. `provide`: refuses invocation before,
  allows it after, takes no value. The seed's clause is in the two-category
  test.
- **Flow.** `tests/flow/test_embryo_liturgy.py` becomes
  `tests/flow/test_capabilities.py` with one test per capability against the
  scripted model. Hatch is today's test with the rows read from `hatch.md`.
  Security promotes a scripted hatch in the fixture, serves the page from the
  fake host's guest script, scripts the three replies, has the driver provision
  from the reply, and checks the three new postconditions plus hatch's seven.
- **E2E.** `tests/e2e/test_phase14_embryo.py` becomes the capabilities phase:
  hatch scripted on the live host, promote, security scripted from the
  promotion. The suite's runtime grows by one promotion and two recipe builds;
  the PR updates the expected gate line in CLAUDE.md.
- **Live.** Each capability's PR ends with its measure: hatch at seven of
  seven and promoted, then security run against the real model and recorded
  under `docs/embryo/security/`.

## 10. Declined, and why

- **A vault object** (`POST /secrets`, #91). Three new axioms (an object, a
  write-only storage class, a permission kind) for a property the relay swap
  gives bootstrap credentials and chain placement gives verb credentials. Row 13
  exists so that the agent, not this document, says whether a shared store is
  needed.
- **Always the latest passing run as the start point.** A new hatch run would
  silently change what every dependent starts from.
- **A per-row settle flag.** Every turn settles; a turn that proposes nothing
  settles at once. A flag would be a mechanism in the words.
- **Prose-only capabilities executed by a pilot model** (#124). Not testable in
  the flow tier and two models per run. A later driver mode.
- **Merging two promoted brains** so a node could start from two parents. The
  filesystem merge exists, but a brain's memory store and policy are not
  three-way mergeable, and the ordered `depends` (§4) gives the same DAG of
  intent without it.
- **Label prefixes per run**, so several capabilities could run concurrently on
  one account. Nothing needs it yet and it touches the membrane's label
  constants, `ingress.star` and the key's `deliver` scope at once.

## 11. Facts checked before the plan

- `speak_liturgy()` in `measure.py:580` hard-codes the ten turns and the repair
  trigger; `verdict()` at `measure.py:736` judges by row label (`"8"`,
  `"9-count-1"`, `"9-count-2"`).
- `hatch.sh:92` mints the relay scope as a list of targets; `ingress.star:13`
  forks the `brain` label; `declarations.py:27` is `CHAIN_PREFIX`.
- `relay.py` (service) forwards `job.forward_headers` and deletes them on settle
  (lines 3, 195, 237); the router has `POST /relay` and `GET /relay/{job_id}`
  only.
- `memory.py:83-124` builds mem0's extraction LLM with the Anthropic key and its
  embedder with the OpenAI key, in-process. The E2E's scripted mode uses a
  deterministic local embedder so no live test depends on a third-party key.
- `proposals.py:110-119` is the block on `requires`; `seed.md:17` and `:27` are
  the two sentences about `requires`.
- No file under `src/`, `embryo/` or `tests/` parses YAML today.
