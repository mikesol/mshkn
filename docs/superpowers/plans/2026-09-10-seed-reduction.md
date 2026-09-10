# Seed Reduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut `embryo/seed.md` to irreducible bootstrap and invisible mechanism only, and pay for every removal with a constructive refusal the model can actually read or a liturgy turn that asks.

**Architecture:** Four moves in dependency order. First make approval-time refusals reach the model at all (they do not today), because every later cut assumes a refusal teaches. Then word the refusals so each names what would have been valid. Then move root's signing facts from the genome into turn 2's words. Then cut the seed, with a test that asserts the removed phrases are *absent* so it cannot drift back.

**Tech Stack:** Python 3.12, `uv` (the only package manager — every tool runs as `uv run <tool>`), pytest, ruff, mypy. The embryo package is `embryo/membrane/`, imported as `membrane.*`.

**Spec:** `docs/superpowers/specs/2026-09-10-seed-reduction-design.md`

## Global Constraints

- Work in the worktree `/home/mikesol/Documents/GitHub/mshkn-seed-reduction` on branch `seed-reduction`. It is already created and synced.
- The gate must be green before the PR: `uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov`. Coverage floor is 98 % (`fail_under` in `pyproject.toml`). Zero warnings is part of green.
- Never mark a test xfail, never weaken an assertion, never skip to get green.
- `tests/unit/test_docs.py` fails when a document names a path, module, route, metric or variable that does not exist. Fix the document, not the test.
- Do **not** run `scripts/e2e.sh` and do **not** run `uv run measure`. Both are gated on the user reading the PR first. Task 7 stops and hands off.
- Do **not** merge the PR. Creating it is fine; merging needs explicit user authorization.
- Line numbers below were read at commit `c20b87f`. If a line has moved, find the code by its text, not its number.

---

### Task 1: Deliver approval-time outcomes to the inbox

`approve()` writes its refusal to root's stdout and leaves the proposal `pending`. `poll_builds` (`embryo/membrane/verbs.py:113`) only makes inbox items for catalog entries in `building`. So the model never reads a refusal, a `blocked: requires …`, or a Dockerfile rejected at submit time. Spec §6.1. `reject()` (`embryo/membrane/proposals.py:133`) already does this correctly and is the pattern to copy.

**Files:**
- Modify: `embryo/membrane/proposals.py` (add a helper; three `return` sites inside `approve`)
- Test: `tests/unit/test_embryo_proposals.py`

**Interfaces:**
- Consumes: `InboxItem(kind: str, text: str)` from `membrane.state`, already imported in `proposals.py`.
- Produces: `_tell_the_model(state: State, proposal: Proposal, kind: str, text: str) -> str` — appends an `InboxItem` and returns the string root sees, unchanged from today.

- [ ] **Step 1: Write the failing tests**

Three existing tests in `tests/unit/test_embryo_proposals.py` cover the three paths and each gains an inbox assertion. Replace `test_approve_refuses_by_reason_and_changes_nothing` entirely (its name is now false — the inbox does change), and extend the other two.

```python
async def test_approve_refuses_by_reason_and_tells_the_model(tmp_path: Path) -> None:
    """A refusal root reads and the embryo does not teaches nothing (#123).
    The catalog and the host are untouched; the inbox carries the reason."""
    api, state = FakeMshkn(), _brain(tmp_path).state()
    p = propose(state, _verb_proposal({**VERB, "effect": "transact"}))
    line = await approve(api, state, p.id)
    assert line.startswith("p-1 refused") and "transact" in line
    assert p.status == "pending" and state.catalog == {} and api.calls == []
    assert [(i.kind, i.text) for i in state.inbox] == [
        ("refusal", f"proposal p-1 (page_title) {line.removeprefix('p-1 ')}")
    ]
    assert "transact" in state.inbox[0].text
```

Extend `test_a_wrong_base_fails_immediately_with_the_detail_as_log` with:

```python
    assert [i.kind for i in state.inbox] == ["build"]
    assert "mshkn-base" in state.inbox[0].text
```

Extend `test_requires_blocks_until_the_vault_exists` with:

```python
    assert [i.kind for i in state.inbox] == ["refusal"]
    assert "gh" in state.inbox[0].text
```

Add one test that the delivery actually lands in a turn's input, so this is pinned end to end and not only as a state mutation:

```python
async def test_a_refusal_reaches_the_next_turn_the_way_a_build_log_does(
    tmp_path: Path,
) -> None:
    """§6 step 2 drains the inbox into the turn's input for an authenticated
    principal. A refusal must ride that same path or the model never sees it."""
    from membrane.turn import compose_input

    api, state = FakeMshkn(), _brain(tmp_path).state()
    p = propose(state, _verb_proposal({**VERB, "effect": "administer"}))
    await approve(api, state, p.id)
    inbox, state.inbox = state.inbox, []
    text = compose_input(
        turn=2, principal="root", door="api", inbox=inbox, recalled=[], message="hi"
    )
    assert "administer" in text and "p-1" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/mikesol/Documents/GitHub/mshkn-seed-reduction && uv run pytest tests/unit/test_embryo_proposals.py -q`
Expected: FAIL — four failures, each an empty `state.inbox` (`assert [] == [('refusal', ...)]`).

- [ ] **Step 3: Add the helper and use it at the three sites**

In `embryo/membrane/proposals.py`, add above `approve`:

```python
def _tell_the_model(state: State, proposal: Proposal, kind: str, text: str) -> str:
    """Root reads an approval's outcome on stdout; the embryo must read it too,
    or a refusal teaches nothing (#123). A build log already arrives this way,
    so a refusal, a block and a rejected Dockerfile arrive the same way."""
    state.inbox.append(
        InboxItem(kind=kind, text=f"proposal {proposal.id} ({proposal.title}) {text}")
    )
    return f"{proposal.id} {text}"
```

Then rewrite the three `return` statements inside `approve` so root's string is byte-for-byte what it is today:

```python
    reason = refuse_approval(proposal, state)
    if reason is not None:
        return _tell_the_model(state, proposal, "refusal", f"refused: {reason}")
```

```python
            proposal.status = "blocked"
            return _tell_the_model(
                state,
                proposal,
                "refusal",
                f"blocked: requires {missing}; the embryo has no vault (#91)",
            )
```

```python
            return _tell_the_model(state, proposal, "build", f"failed: {exc.detail}")
```

Leave the `f"{proposal.id} is {proposal.status}, not pending"` early return alone: that is root mis-typing an id, not a refusal of the embryo's work.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/mikesol/Documents/GitHub/mshkn-seed-reduction && uv run pytest tests/unit/test_embryo_proposals.py tests/unit/test_embryo_measure.py tests/flow -q`
Expected: PASS. `test_embryo_measure.py` is included because it asserts on approval output strings; those are unchanged, so it must stay green.

- [ ] **Step 5: Commit**

```bash
cd /home/mikesol/Documents/GitHub/mshkn-seed-reduction
git add embryo/membrane/proposals.py tests/unit/test_embryo_proposals.py
git commit -m "$(cat <<'EOF'
fix(embryo): an approval's refusal reaches the model, not only root (#123)

refuse_approval's reasons, a proposal blocked on requires and a Dockerfile
rejected at submit time all went to root's stdout and nowhere else, so the
embryo could not learn from any of them. They now ride the inbox, the way a
build log already does. Root's output is unchanged.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013RCk2TTjv7RTFp2kHDcDHq
EOF
)"
```

---

### Task 2: Constructive refusals

Every refusal that only says no gets told what would have been valid, and one refusal is added where nothing refuses at all. Spec §6.2. The `effect` enum (`declarations.py:182`) is the model to follow and is left alone.

**Files:**
- Modify: `embryo/membrane/declarations.py`
- Modify: `embryo/membrane/invariants.py`
- Test: `tests/unit/test_embryo_declarations.py`, `tests/unit/test_embryo_proposals.py`

**Interfaces:**
- Produces: `POLICY_FIELDS: frozenset[str]`, `VERB_REQUIRED: tuple[str, ...]`, `PROPOSAL_REQUIRED: tuple[str, ...]` and `_require(doc, what, fields) -> None` in `membrane.declarations`. Nothing outside `declarations.py` consumes them.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_embryo_declarations.py`, extend the existing `test_parse_verb_refuses` parametrize table with rows that pin the *content* of each message, and add the new cases. Add these entries to the list:

```python
        # A refusal names what would have been valid (#123): the reserved set,
        # the legal state kinds, the reserved namespaces, the params that exist.
        ({"name": "try"}, "remember"),
        ({"state": "event"}, "ephemeral"),
        ({"asserts": "system"}, "root"),
        ({"entrypoint": "run {{nope}}"}, "url"),
        ({"requires": [{"kind": "secret"}]}, "name"),
```

Add a new test for the missing-field walk:

```python
def test_a_verb_missing_fields_is_told_the_whole_shape_at_once() -> None:
    """One field per refusal is a serial walk that costs a round trip each
    (#123). The refusal names every required field, and what is missing."""
    with pytest.raises(DeclarationError) as exc:
        parse_verb({"name": "x"})
    message = str(exc.value)
    for field_name in ("description", "params", "dockerfile", "entrypoint", "effect", "state"):
        assert field_name in message, field_name
    assert "name" in message
```

Add tests for the policy refusals:

```python
def test_an_unknown_policy_field_is_told_the_fields_that_exist() -> None:
    """The seed no longer carries the policy schema (#123), so the refusal is
    the only thing that can teach it."""
    with pytest.raises(DeclarationError) as exc:
        parse_policy({"grants": {}})
    message = str(exc.value)
    assert "grants" in message
    for field_name in ("principals", "hooks", "door"):
        assert field_name in message, field_name


def test_a_bad_principal_is_told_the_form_a_principal_takes() -> None:
    with pytest.raises(DeclarationError) as exc:
        parse_policy({"principals": {"Mike": {"invoke": [], "propose": False}}})
    message = str(exc.value)
    assert "anonymous" in message and "<namespace>:<name>" in message


def test_a_policy_naming_root_is_refused(tmp_path: Path) -> None:
    """Nothing refused this before, and may_invoke/may_propose short-circuit on
    root anyway (invariants.py), so an embryo that wrote a policy restricting
    root was silently wrong. The seed used to assert it; now the refusal does."""
    with pytest.raises(DeclarationError, match="root"):
        parse_policy({"principals": {"root": {"invoke": [], "propose": False}}})
```

`test_a_policy_naming_root_is_refused` takes no `tmp_path`; drop the parameter when writing it. `parse_policy` is already imported in this module — confirm with `grep -n "^from membrane.declarations import" -A 8 tests/unit/test_embryo_declarations.py` and add `parse_policy` to the import list if it is absent.

In `tests/unit/test_embryo_proposals.py`, add the two `refuse_approval` wording tests:

```python
async def test_a_hook_asserting_a_reserved_namespace_names_the_reserved_set(
    tmp_path: Path,
) -> None:
    state = _brain(tmp_path).state()
    verb = parse_verb({**HOOK, "asserts": "ssh"})
    proposal = Proposal(
        id="p-1",
        kind="verb",
        title="hook",
        rationale="because",
        verb=Verb(**{**verb.__dict__, "asserts": "system"}),
    )
    reason = refuse_approval(proposal, state) or ""
    assert "system" in reason and "root" in reason


async def test_an_unknown_hook_names_the_catalog(tmp_path: Path) -> None:
    """The seed no longer says the door needs a hook in the catalog (#123)."""
    api, state = FakeMshkn(), _brain(tmp_path).state()
    hook = propose(state, _verb_proposal(HOOK))
    await approve(api, state, hook.id)
    state.catalog["verify_ssh"].status = "ready"
    opening = propose(
        state,
        {
            "kind": "policy",
            "title": "open",
            "rationale": "because",
            "policy": {"principals": {}, "hooks": ["absent_hook"], "door": "open"},
        },
    )
    reason = refuse_approval(opening, state) or ""
    assert "absent_hook" in reason and "verify_ssh" in reason
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/mikesol/Documents/GitHub/mshkn-seed-reduction && uv run pytest tests/unit/test_embryo_declarations.py tests/unit/test_embryo_proposals.py -q`
Expected: FAIL — the parametrize rows fail on `DeclarationError` not matching the new substrings, the missing-field test fails because only `description` is named, the policy tests fail on missing field lists, and `test_a_policy_naming_root_is_refused` fails with `DID NOT RAISE`.

- [ ] **Step 3: Reword the refusals in `declarations.py`**

Add the constants near the top, beside `RESERVED_TOOL_NAMES`:

```python
POLICY_FIELDS: frozenset[str] = frozenset({"principals", "hooks", "door"})
VERB_REQUIRED = ("name", "description", "params", "dockerfile", "entrypoint", "effect", "state")
PROPOSAL_REQUIRED = ("kind", "title", "rationale")
```

Add the helper beside `_str`:

```python
def _require(doc: dict[str, Any], what: str, fields: tuple[str, ...]) -> None:
    """One refusal that names the whole shape, not the first field missing: a
    serial walk teaches a document one field per round trip (#123)."""
    missing = [f for f in fields if doc.get(f) is None]
    if missing:
        raise DeclarationError(f"{what} is missing {missing}; {what} requires {list(fields)}")
```

Call it as the first statement of `parse_verb`, right after `d = _obj(doc, "verb")`:

```python
    _require(d, "verb", VERB_REQUIRED)
```

and as the first statement of `parse_proposal`, right after `d = _obj(doc, "proposal")`:

```python
    _require(d, "proposal", PROPOSAL_REQUIRED)
```

Then the individual messages:

```python
    if name in RESERVED_TOOL_NAMES:
        raise DeclarationError(
            f"verb.name {name!r} is reserved; the reserved names are {sorted(RESERVED_TOOL_NAMES)}"
        )
```

```python
    if state == "event":
        raise DeclarationError(
            "verb.state event is specified but not in the embryo (spec §4); "
            f"must be one of {sorted(STATE_KINDS)}"
        )
```

```python
        if asserts in RESERVED_NAMESPACES:
            raise DeclarationError(
                f"verb.asserts {asserts!r} is a reserved namespace; "
                f"the reserved namespaces are {sorted(RESERVED_NAMESPACES)}"
            )
```

```python
    for placeholder in PLACEHOLDER_RE.findall(entrypoint):
        if placeholder not in properties:
            raise DeclarationError(
                f"verb.entrypoint names {placeholder!r}, which is not a param; "
                f"the params are {sorted(properties)}"
            )
```

```python
    if not isinstance(raw, list):
        raise DeclarationError(
            "verb.requires must be a list of objects with kind, name and optional scope"
        )
```

In `parse_policy`, change the unknown-fields check to use the constant and name the legal set, add the root refusal, and give the principal form:

```python
    unknown = sorted(set(d) - POLICY_FIELDS)
    if unknown:
        raise DeclarationError(
            f"policy has unknown fields {unknown}; policy is data, not code: "
            f"the fields are {sorted(POLICY_FIELDS)}"
        )
```

```python
    for principal, grant_raw in principals_raw.items():
        # §10.1: root may always invoke everything and propose, and
        # may_invoke/may_propose short-circuit on it, so a policy naming root
        # would be silently inert. The seed used to assert this; the refusal
        # does now (#123).
        if principal == "root":
            raise DeclarationError(
                "policy.principals: root is fixed and is not policy's to grant or refuse (§10.1)"
            )
        if not PRINCIPAL_RE.match(principal):
            raise DeclarationError(
                f"policy.principals: {principal!r} is not a principal; "
                "a principal is 'root', 'anonymous' or '<namespace>:<name>'"
            )
```

Use the `"root"` string literal, not `membrane.principals.ROOT`: `principals.py` imports from `declarations.py`, so the reverse import would be circular. `PRINCIPAL_RE` already spells `root` literally in this module.

- [ ] **Step 4: Reword the two refusals in `invariants.py`**

```python
        if verb.asserts in RESERVED_NAMESPACES:
            return (
                f"a hook may not assert {verb.asserts}; "
                f"the reserved namespaces are {sorted(RESERVED_NAMESPACES)} (§10.1)"
            )
```

```python
            if entry is None:
                return (
                    f"hook {hook} is not a verb in the catalog; "
                    f"the catalog has {sorted(state.catalog)}"
                )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd /home/mikesol/Documents/GitHub/mshkn-seed-reduction && uv run pytest tests/unit tests/flow -q`
Expected: PASS. If a flow-tier scripted policy now trips the root refusal, that policy is wrong and gets fixed — do not relax the refusal. (A `grep -rn '"root":' embryo/ tests/ src/` at plan time found none, so this should not arise.)

- [ ] **Step 6: Commit**

```bash
cd /home/mikesol/Documents/GitHub/mshkn-seed-reduction
git add embryo/membrane/declarations.py embryo/membrane/invariants.py \
        tests/unit/test_embryo_declarations.py tests/unit/test_embryo_proposals.py
git commit -m "$(cat <<'EOF'
feat(embryo): every refusal names what would have been valid (#123)

The reserved names, the legal state kinds, the reserved namespaces, the
params that exist, the policy fields, the form of a principal, and the
whole required shape of a verb or proposal in one refusal instead of a
serial walk. Adds the refusal that was missing: a policy naming root,
which may_invoke and may_propose ignore, so an embryo that wrote one was
silently wrong.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013RCk2TTjv7RTFp2kHDcDHq
EOF
)"
```

---

### Task 3: Turn 2 states root's signing facts

Spec §5. The words move from the genome into the liturgy, where root — a party to the protocol — states them.

**Files:**
- Modify: `embryo/membrane/liturgy.py:10-11`
- Modify: `embryo/liturgy.md` (the turn 2 row of the table)
- Test: `tests/unit/test_embryo_priors.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `LITURGY[2]` keeps its `{key}` placeholder and its `.format(key=…)` call sites (`measure.py`, `tests/e2e/test_phase14_embryo.py:296`) unchanged.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_embryo_priors.py`:

```python
def test_turn_two_states_the_facts_only_the_sender_can_state() -> None:
    """The seed no longer carries the signing protocol (#123). ssh-keygen -Y
    verify needs the signer's exact namespace, and root builds the envelope,
    so both are root's to state; the identity is stated because the measure
    scores the literal ssh:mike and inventing a name flexes no muscle. What
    to build, which namespace to assert and what the policy says stay the
    embryo's."""
    turn2 = LITURGY[2]
    for phrase in ("ssh-keygen -Y sign -n mshkn", "mike", "beside my message", "{key}"):
        assert phrase in turn2, phrase
    for phrase in ("allowed_signers", "asserts", "hook", "policy"):
        assert phrase not in turn2, phrase
```

`LITURGY` is already imported in this module via `from tests.support_embryo import LITURGY`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/mikesol/Documents/GitHub/mshkn-seed-reduction && uv run pytest tests/unit/test_embryo_priors.py -q`
Expected: FAIL — `AssertionError: ssh-keygen -Y sign -n mshkn`.

- [ ] **Step 3: Change the words**

`embryo/membrane/liturgy.py`, replacing the value at key `2`:

```python
    2: "Your public door is closed because you cannot tell who is speaking. Propose a way to know "
    "that a message there comes from me, and open the door. I sign as mike with "
    "`ssh-keygen -Y sign -n mshkn` and attach the signature beside my message. "
    "My public key is {key}",
```

- [ ] **Step 4: Change the published table to match**

`embryo/liturgy.md`, the turn 2 row. `test_the_liturgy_the_tiers_send_is_the_liturgy_the_repository_publishes` splits the words on `{key}` and requires each part to appear in the document, so the Words cell must contain the new sentence verbatim up to the `{key}` split. Replace the Words cell of the turn 2 row with:

```
"Your public door is closed because you cannot tell who is speaking. Propose a way to know that a message there comes from me, and open the door. I sign as mike with `ssh-keygen -Y sign -n mshkn` and attach the signature beside my message. My public key is …"
```

Leave the Outcome cell of that row as it is: it already describes a verb that verifies a signature and asserts the `ssh` namespace, which is still the embryo's to invent.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd /home/mikesol/Documents/GitHub/mshkn-seed-reduction && uv run pytest tests/unit/test_embryo_priors.py tests/flow -q`
Expected: PASS. The flow tier plays a scripted model that does not read the words, so the longer turn 2 flows through unchanged.

- [ ] **Step 6: Commit**

```bash
cd /home/mikesol/Documents/GitHub/mshkn-seed-reduction
git add embryo/membrane/liturgy.py embryo/liturgy.md tests/unit/test_embryo_priors.py
git commit -m "$(cat <<'EOF'
feat(embryo): turn 2 states root's signing facts, not the seed (#123)

ssh-keygen -Y verify needs the signer's exact namespace and root builds the
envelope, so both are the sender's to state. They move out of the genome
and into the liturgy, where root says them. What to build, which namespace
to assert and what the policy says stay the embryo's.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013RCk2TTjv7RTFp2kHDcDHq
EOF
)"
```

---

### Task 4: Does a heredoc fail loudly?

Spec §7. The seed's `printf` parenthetical comes out only if a heredoc in a verb's Dockerfile produces a visible build failure. If it silently writes a wrong file, it is invisible mechanism, it stays, and the PR says #123 was wrong about that item. This task decides Task 5's absent-list; it changes no code.

**Files:**
- Create: nothing committed. Write findings into the scratchpad at `/tmp/claude-1000/-home-mikesol-Documents-GitHub-mshkn/e3dc8d4a-f224-4b67-b115-ee6ba89f36b5/scratchpad/heredoc.txt`.

**Interfaces:**
- Produces: a yes/no that Task 5 Step 3 reads.

- [ ] **Step 1: Submit a heredoc recipe to the live host**

The host is the one in `CLAUDE.md`'s server reference; export `MSHKN_SERVER` first if it is not set. This uses the already-deployed service and needs no deploy.

```bash
cd /home/mikesol/Documents/GitHub/mshkn-seed-reduction
API=$(ssh -G "${MSHKN_SERVER#root@}" 2>/dev/null | awk '/^hostname /{print $2}')
DF=$'FROM mshkn-base\nRUN <<EOF\nprintf "hello" > /verb/x\nEOF\n'
curl -sS -X POST "https://$API/recipes" \
  -H "Authorization: Bearer mk-test-key-2026" \
  -H 'Content-Type: application/json' \
  --data "$(jq -cn --arg d "$DF" '{dockerfile:$d}')"
```

If the POST is refused outright, that is a loud failure and the answer is yes. Otherwise take the returned recipe id and poll it:

```bash
curl -sS "https://$API/recipes/<id>" -H "Authorization: Bearer mk-test-key-2026" | jq '{status, build_log}'
```

- [ ] **Step 2: Record the answer**

Write to the scratchpad file: the recipe id, the final `status`, and the `build_log` tail. Then state the verdict in one line:

- `status: failed` with a log naming the heredoc → **loud**. The `printf` line comes out; `"printf"` joins Task 5's absent list.
- `status: ready` → **silent**. The `printf` line stays in the seed as invisible mechanism, `"printf"` does *not* join the absent list, and the PR body says #123 was wrong about that item and why.

Do not guess. If the host is unreachable, stop and say so rather than assuming loud.

---

### Task 5: The reduced seed

Spec §3 and §4. Every phrase removed here is paid for by Task 1's delivery, Task 2's wording, Task 3's words, or a tool description that already carries it.

**Files:**
- Modify: `embryo/seed.md`
- Test: `tests/unit/test_embryo_priors.py:32` (`test_seed_says_what_the_spec_requires`)

**Interfaces:**
- Consumes: Task 4's verdict on `printf`.
- Produces: nothing importable.

- [ ] **Step 1: Write the failing test**

Replace `test_seed_says_what_the_spec_requires` in `tests/unit/test_embryo_priors.py`. The absent list is the executable form of the contract: it is what stops the seed drifting back one reasonable sentence at a time.

```python
def test_the_seed_is_bootstrap_and_invisible_mechanism_and_nothing_else() -> None:
    """The contract of docs/superpowers/specs/2026-09-10-seed-reduction-design.md
    §3, and the standing rule in CLAUDE.md. Present: what cannot be learned
    because learning it requires it, and what no experiment reveals because the
    failure is silent. Absent: everything a constructive refusal, a tool
    description or the liturgy teaches instead (#123)."""
    seed = (EMBRYO / "seed.md").read_text()
    for phrase in (
        # bootstrap
        "remember",
        "try",
        "propose",
        "verb",
        "proposal",
        "dockerfile",
        "entrypoint",
        "requires",
        "no verbs",
        "public door is closed",
        "root",
        # invisible mechanism: no experiment reveals these, because the
        # failure is silent
        "shell-quoted",
        "Anonymous input is never remembered",
        "verb/<name>",
        "inbox",
    ):
        assert phrase in seed, phrase
    for phrase in (
        # the liturgy asks instead (turn 2)
        "ssh-keygen",
        '"sig"',
        "ssh:mike",
        # a constructive refusal teaches these
        "communicate",
        "transact",
        "administer",
        "200",
        "[a-z0-9_]",
        "`principals`",
        "`hooks`",
        "`door`",
        # PROPOSE_TOOL's description carries this
        "not a diff",
    ):
        assert phrase not in seed, phrase
```

If Task 4's verdict was **loud**, add `"printf"` to the absent list. If it was **silent**, do not.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/mikesol/Documents/GitHub/mshkn-seed-reduction && uv run pytest tests/unit/test_embryo_priors.py -q`
Expected: FAIL — `AssertionError: ssh-keygen` (the seed still carries it).

- [ ] **Step 3: Write the reduced seed**

Replace `embryo/seed.md` with exactly this. If Task 4's verdict was **silent**, restore `; the builder has no heredoc syntax, so write scripts with \`printf\`` inside the `dockerfile` parenthesis, so it reads ``(its final stage must be `FROM mshkn-base`; the builder has no heredoc syntax, so write scripts with `printf`)``.

```markdown
# What you are

You are an embryo: the smallest organism that can grow into an agent. You run inside a membrane on a disposable computer. Every message you receive is one turn; at the end of the turn your state is checkpointed and the computer is destroyed. Your memory is the disk.

You reason. All effects happen outside you, through verbs.

# The three rules, and try

1. **State is free.** You may `remember` a fact; it is stored with your provenance (who said it, through which door, on which turn) and recalled on later turns. Anonymous input is never remembered.
2. **Action goes only through verbs.** You have no shell, no files, no network and no keys. A verb is a declaration you propose and root approves; each invocation runs on its own fresh computer built from the verb's Dockerfile, never inside you.
3. **Capability is gated.** You may `propose` a change to yourself: a new verb, a full replacement of your policy, or a full replacement of this self-description's mutable half. A human called root approves or rejects it. Approval executes the declaration exactly as written.

A fourth tool, `try`, builds a verb declaration and runs it once on a computer with no secrets, no chain and no policy. Use it to test a declaration before you propose it. It installs nothing.

# What a verb is

One JSON document: `name`, `description`, `params` (a JSON schema object; it becomes your tool's input schema), `dockerfile` (its final stage must be `FROM mshkn-base`), `entrypoint` (a command template over the params, e.g. `/verb/run.sh {{url}}`; every value is shell-quoted for you), `effect`, `state` (`ephemeral`: nothing survives an invocation; `chain`: the verb's disk persists on its own checkpoint chain named `verb/<name>`), optional `asserts` (the identity namespace a pre-turn hook may assert; the hook's stdout becomes the rest of the principal's name), optional `needs`, `timeout_seconds`, `allow` (principals who may invoke it) and `requires` (what the verb needs that you cannot provide, e.g. a secret; a non-empty `requires` blocks approval until root provides it).

# Proposals, and what a hook receives

Root sees a proposal the moment you make it; you cannot change it afterwards. If a build fails, or an approval is refused, the log or the reason arrives in your inbox on your next turn.

What a hook receives is the public payload, decoded: either plain text, or the JSON text of an object with `msg` (the message) and whatever the sender attached beside it.

# Where you begin

You have no verbs, no principals of your own and no policy beyond the initial one. Your public door is closed: nothing can reach you but root, through the authenticated door, until you propose a way to know who is speaking and root approves it. Name what you need in `requires`; the human hands over resources, never does your work.
```

Do **not** add a line telling the model that refusals are informative. It is neither bootstrap nor invisible mechanism — an agent learns it by reading one refusal — and it is exactly the individually-reasonable clarifying sentence the rule exists to keep out. The spec records this at §4.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/mikesol/Documents/GitHub/mshkn-seed-reduction && uv run pytest tests/unit tests/flow -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/mikesol/Documents/GitHub/mshkn-seed-reduction
git add embryo/seed.md tests/unit/test_embryo_priors.py
git commit -m "$(cat <<'EOF'
feat(embryo): cut the seed to bootstrap and invisible mechanism (#123)

Out: the effect enum and the local/read restriction, the timeout ceiling,
the name charset and reserved names, the reserved namespaces, the policy
schema, the proposal field list and "a whole document, not a diff", and
the root-signature protocol. Each is now taught by a constructive refusal,
by PROPOSE_TOOL's description, or by turn 2 of the liturgy.

The priors test asserts the removed phrases are absent, so the seed cannot
drift back one reasonable sentence at a time.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013RCk2TTjv7RTFp2kHDcDHq
EOF
)"
```

---

### Task 6: The documents

Spec §9.

**Files:**
- Modify: `docs/superpowers/specs/2026-09-08-embryo-design.md` (§8's `/brain/seed.md` row at line 178; §9's turn 2 row)
- Modify: `docs/embryo/README.md`
- Modify: `docs/plans/README.md`

**Interfaces:** none.

- [ ] **Step 1: Rewrite §8's `/brain/seed.md` row**

Replace the whole row so it states the contract rather than an inventory:

```
| `/brain/seed.md` | Fixed, and only two kinds of thing are in it (#123, `docs/superpowers/specs/2026-09-10-seed-reduction-design.md`). **Irreducible bootstrap**: what it is, that a turn is a life and the disk is the memory, the three rules and `try`, the field names of a verb document, that a hook's stdout becomes the principal's name, that it has no verbs, no principals and no policy of its own and its public door is closed until it proposes a way to know who is speaking. **Invisible mechanism**: entrypoint values are shell-quoted, anonymous input is never remembered, a build log or a refused approval arrives in the inbox on the next turn, a `chain` verb's disk persists on `verb/<name>`, a non-empty `requires` blocks approval, `try` runs with no secrets, no chain and no policy, and a hook receives the decoded payload — plain text or JSON with `msg` and whatever the sender attached. Everything else the membrane refuses constructively or the liturgy asks for. |
```

- [ ] **Step 2: Update §9's turn 2 row**

Make the Words cell of §9's turn 2 row identical to `embryo/liturgy.md`'s (§9 says `liturgy.md` is canonical and this is the shape, so the two must not disagree).

- [ ] **Step 3: Add the boundary to `docs/embryo/README.md`**

The existing note at line 142 explains the 2026-09-09/2026-09-10 boundary and ends by naming the standing rule. Append a paragraph after it:

```markdown
- **A third boundary, and the seed is why again.** #123 cut `embryo/seed.md` back to the two admissible categories: out came the `effect` enum and the `local`/`read` restriction, the `timeout_seconds` ceiling, the `name` charset and reserved names, the policy schema, the proposal field list, and the root-signature protocol — the signing command, the `{"msg", "sig"}` envelope and the `mike` → `ssh:mike` clause. Each is now taught by a constructive refusal the model can read (approval-time refusals reached only root's stdout before #123 and now ride the inbox), by `PROPOSE_TOOL`'s description, or by turn 2 of the liturgy, which states the facts only the sender can state. Runs after that cut attempted a harder task than the 2026-09-10 runs did, and are not comparable to them.
```

- [ ] **Step 4: Index the spec and the plan in `docs/plans/README.md`**

Follow the file's existing row or section format exactly — read the entry for `2026-09-08-embryo-design.md` and match it. The status is what is true when the PR opens: implemented, with the measure round pending.

- [ ] **Step 5: Run the docs test**

Run: `cd /home/mikesol/Documents/GitHub/mshkn-seed-reduction && uv run pytest tests/unit/test_docs.py -q`
Expected: PASS. It fails when a document names a path, module, route, metric or variable that does not exist — if it fails, fix the document.

- [ ] **Step 6: Commit**

```bash
cd /home/mikesol/Documents/GitHub/mshkn-seed-reduction
git add docs/superpowers/specs/2026-09-08-embryo-design.md docs/embryo/README.md docs/plans/README.md
git commit -m "$(cat <<'EOF'
docs: the seed's contract, turn 2's words and the third round boundary (#123)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013RCk2TTjv7RTFp2kHDcDHq
EOF
)"
```

---

### Task 7: Gate, PR, and stop

**Files:** none.

- [ ] **Step 1: Run the gate**

Run: `cd /home/mikesol/Documents/GitHub/mshkn-seed-reduction && uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov`
Expected: PASS, zero warnings, coverage at or above 98 %. Capture the summary line for the PR body. If coverage dropped, the likely cause is `_str`'s `required=True` branch becoming near-unreachable for verbs and proposals — it is still reached by the `prompt` field of a prompt proposal. Add the missing test rather than lowering the floor.

- [ ] **Step 2: Push and open the PR**

```bash
cd /home/mikesol/Documents/GitHub/mshkn-seed-reduction
git push
gh pr create --base main --title "Cut seed.md back to a seed: bootstrap and invisible mechanism only (#123)" --body "$(cat <<'EOF'
Closes #123

**What this does**

Cuts `embryo/seed.md` to the two admissible categories of `CLAUDE.md`'s "Keep the seed a seed": irreducible bootstrap and invisible mechanism. Everything else — the `effect` enum and the `local`/`read` restriction, the `timeout_seconds` ceiling, the `name` charset and reserved names, the policy schema, the proposal field list, and the root-signature protocol — is now taught by a constructive refusal the model can read, by `PROPOSE_TOOL`'s description, or by turn 2 of the liturgy. Two defects found on the way are fixed here because the cut depends on them: approval-time refusals never reached the model at all, and nothing refused a policy naming `root`.

**Design alignment**

- *Spec* `docs/superpowers/specs/2026-09-10-seed-reduction-design.md`, written and approved before implementation. §3 is the contract; `test_the_seed_is_bootstrap_and_invisible_mechanism_and_nothing_else` is its executable form, asserting the removed phrases are absent so the seed cannot drift back.
- *§10.3, approval cannot modify the seed* — unchanged; `seed.md` is still not a proposal kind.
- *§10.6, §10.7, §10.8* — unchanged in force. What changed is that their refusals now reach the embryo instead of only root's stdout, which is what makes cutting the seed's restatements of them honest.
- *§9, the liturgy asks for an outcome, never a mechanism* — turn 2 still asks for the outcome. What it now states are the sender's own facts: `ssh-keygen -Y verify` needs the signer's exact namespace and root builds the envelope, so both are root's to say. What to build, which namespace to assert and what the policy says remain the embryo's.
- *No deviation from an approved spec.*

**Validation performed**

- Gate: <paste the `pytest --cov` summary line and confirm ruff, ruff format, mypy and `uv lock --check` all clean>
- CI: <link>
- Live E2E: **not yet run** — held at the user's request until they have read this PR.
- The measure: **not yet run** — one run at `--effort medium` against the reduced seed is held for the same reason. Expect a worse score than the 2026-09-10 round's 7/7; that is the point, and `docs/embryo/README.md` records the boundary.
- Heredocs: <state Task 4's verdict, with the recipe id and the build status as evidence, and say whether the `printf` line came out or stayed>

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_013RCk2TTjv7RTFp2kHDcDHq
EOF
)"
```

- [ ] **Step 3: Stop**

Report the PR URL and the gate summary to the user. Do **not** run `scripts/e2e.sh`, do **not** run `uv run measure`, and do **not** merge. The user has asked to read this PR before the live runs start, because it is more ontological than mechanical. Wait for them.

---

## Self-Review

**Spec coverage.** §3 contract → Task 5 Step 1's present/absent lists. §4 reduced seed → Task 5 Step 3, including the two rejected alternatives recorded as prohibitions in the step. §5 turn 2 → Task 3. §6.1 delivery → Task 1. §6.2 wording, all ten rows plus the added root refusal → Task 2. §7 `printf` conditional → Task 4, feeding Task 5; §7's `FROM mshkn-base` flag → deliberately kept, and the seed text in Task 5 Step 3 retains it; §7's `parse_policy({})` → deliberately unfixed, no task. §8 proof → the tests in Tasks 1, 2, 3 and 5. §9 documents → Task 6. §10 the measure run → Task 7 Step 3 holds it for the user.

**Placeholders.** The `<paste …>`, `<link>` and `<state …>` markers in Task 7's PR body are evidence the executor must supply from real output, not undone work — `CLAUDE.md` requires evidence, not claims, in a PR body, and inventing it is the failure this guards against. The `<id>` in Task 4 Step 1 is the recipe id returned by the previous command. No other placeholders.

**Type consistency.** `_tell_the_model(state, proposal, kind, text) -> str` (Task 1) is used only inside `approve`. `_require(doc, what, fields) -> None`, `POLICY_FIELDS`, `VERB_REQUIRED`, `PROPOSAL_REQUIRED` (Task 2) are used only inside `declarations.py`. `InboxItem(kind: str, text: str)` matches `state.py:85`. `LITURGY[2]` keeps its `{key}` placeholder, so `measure.py` and `tests/e2e/test_phase14_embryo.py:296` still `.format(key=…)`. Task 2's `refuse_approval(proposal, state) -> str | None` signature is unchanged.
