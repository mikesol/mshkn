# Coding Capability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Grow node #161 of the capability DAG — a capability whose subject is code that outlives the turn that wrote it — and give the driver the one hook it needs to judge that from outside every door.

**Architecture:** Six rows (15–21) on top of security's promotion ask the agent for a program that totals amounts, use it, meet it with input it was not written for, ask for the behaviour root wants, use it again, and ask what command root would run himself. After the final listing a new module hook, `verify(doors, turns, final, log)`, forks the head of the verb's chain and runs that command over three files root writes, with the account key and through no door. The checks `runs_again` and `fixed` read those probes; nothing is judged on the transcript.

**Tech Stack:** Python 3.12, `uv`, pytest (unit/flow/e2e tiers), httpx, the embryo membrane under `embryo/`. Design: `docs/superpowers/specs/2026-09-17-coding-design.md`.

**Working tree:** `~/work/mshkn-coding`, branch `capability/coding`, based on `ed51601`.

**The gate, for every task that says "run the gate":**

```bash
cd ~/work/mshkn-coding && uv run --python /usr/bin/python3 ruff check . \
  && uv run --python /usr/bin/python3 ruff format --check . \
  && uv run --python /usr/bin/python3 mypy \
  && uv run --python /usr/bin/python3 pytest --cov -q
```

`--python /usr/bin/python3` is required on this box: the uv-managed CPython lacks `os.pidfd_open` and fails five Firecracker tests on a clean `main` too. Coverage floor is 98% (`fail_under`).

---

### Task 1: Move `sse_stdout` and the upload helper into the driver

Coding's module needs both, and capability modules are loaded one at a time by path (`load_module` does not put them in `sys.modules`), so `coding.py` cannot import them from `security.py`. They move to `embryo/membrane/capability.py` and security imports them from there. Its module attributes keep their names, so `tests/unit/test_embryo_security.py:119` keeps passing unchanged.

**Files:**
- Modify: `embryo/membrane/capability.py` (add two helpers near `paths_in`, around line 76)
- Modify: `embryo/capabilities/security.py:119-145` (delete both definitions, import instead)
- Test: `tests/unit/test_embryo_capability.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_embryo_capability.py`:

```python
def test_sse_stdout_reads_the_stdout_lines_and_the_exit_code() -> None:
    from membrane.capability import sse_stdout

    stream = "".join(
        f"event: {e}\r\ndata: {d}\r\n\r\n"
        for e, d in (("stdout", "a"), ("stderr", "x"), ("stdout", "b"), ("exit", "0"))
    )
    assert sse_stdout(stream) == ("a\nb", 0)
    assert sse_stdout("event: stdout\ndata: only\n\nevent: exit\ndata: 3\n\n") == ("only", 3)
    assert sse_stdout("") == ("", None)
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd ~/work/mshkn-coding && uv run --python /usr/bin/python3 pytest \
  tests/unit/test_embryo_capability.py::test_sse_stdout_reads_the_stdout_lines_and_the_exit_code -q
```

Expected: FAIL, `ImportError: cannot import name 'sse_stdout' from 'membrane.capability'`.

- [ ] **Step 3: Move the two helpers**

In `embryo/membrane/capability.py`, immediately after `paths_in`, add:

```python
def sse_stdout(text: str) -> tuple[str, int | None]:
    """The stdout lines and the exit code of an exec stream (`event:`/`data:` pairs)."""
    out: list[str] = []
    code: int | None = None
    event = ""
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if line.startswith("event: "):
            event = line[len("event: ") :]
        elif line.startswith("data: "):
            data = line[len("data: ") :]
            if event == "stdout":
                out.append(data)
            elif event == "exit":
                code = int(data)
    return "\n".join(out), code


async def upload(doors: Any, computer_id: str, path: str, data: bytes) -> None:
    """A file onto a computer, with the account key and through no door: what a
    capability module's scaffolding writes before it runs something."""
    response = await doors.api.post(
        f"/computers/{computer_id}/upload",
        params={"path": path},
        content=data,
        headers={"content-type": "application/octet-stream"},
    )
    response.raise_for_status()
```

In `embryo/capabilities/security.py`, delete `sse_stdout` (lines 119-135) and `_upload` (lines 137-145), add the import beside the existing membrane imports:

```python
from membrane.capability import sse_stdout, upload
```

and replace the three `_upload(` call sites (lines 179, 214, 215) with `upload(`.

- [ ] **Step 4: Run the tests**

```bash
cd ~/work/mshkn-coding && uv run --python /usr/bin/python3 pytest \
  tests/unit/test_embryo_capability.py tests/unit/test_embryo_security.py \
  tests/flow/test_capabilities.py -q
```

Expected: PASS, including `test_sse_stdout_reads_crlf_and_lf_streams`, which reaches the moved function through `security.sse_stdout` and proves the re-export works.

- [ ] **Step 5: Commit**

```bash
cd ~/work/mshkn-coding && git add embryo/membrane/capability.py embryo/capabilities/security.py tests/unit/test_embryo_capability.py
git commit -m "Two helpers a second capability module needs move to the driver"
```

---

### Task 2: The `verify` hook

**Files:**
- Modify: `embryo/membrane/capabilities.py` (module docstring, and a `Verify` Protocol beside `Prepare` at line 55)
- Modify: `embryo/membrane/capability.py` (a `run_verify` helper, and one call after `record.final_list(final)` at line 1874)
- Test: `tests/unit/test_embryo_capability.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_embryo_capability.py`:

```python
async def test_run_verify_hands_the_module_the_turns_and_the_listing(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from membrane.capability import run_verify

    seen: dict[str, Any] = {}

    async def verify(doors: Any, turns: Any, final: Any, log: Any) -> None:
        seen.update({"doors": doors, "turns": turns, "final": final})
        log.write("probed\n")

    log = io.StringIO()
    module = SimpleNamespace(verify=verify)
    await run_verify(module, "doors", ["a-turn"], {"catalog": {}}, log)
    assert seen == {"doors": "doors", "turns": ["a-turn"], "final": {"catalog": {}}}
    assert log.getvalue() == "probed\n"


async def test_run_verify_is_a_no_op_without_a_module_or_without_the_hook() -> None:
    from types import SimpleNamespace

    from membrane.capability import run_verify

    log = io.StringIO()
    await run_verify(None, "doors", [], {}, log)
    await run_verify(SimpleNamespace(), "doors", [], {}, log)
    assert log.getvalue() == ""


async def test_a_verify_that_raises_is_recorded_and_does_not_end_the_run() -> None:
    from types import SimpleNamespace

    from membrane.capability import run_verify

    async def verify(doors: Any, turns: Any, final: Any, log: Any) -> None:
        raise RuntimeError("no chain")

    log = io.StringIO()
    await run_verify(SimpleNamespace(verify=verify), "doors", [], {}, log)
    assert "verify failed: RuntimeError: no chain" in log.getvalue()
```

If `io`, `Any` or `Path` are not already imported in that file, add them.

- [ ] **Step 2: Run them and watch them fail**

```bash
cd ~/work/mshkn-coding && uv run --python /usr/bin/python3 pytest \
  tests/unit/test_embryo_capability.py -k run_verify -q
```

Expected: FAIL, `ImportError: cannot import name 'run_verify'`.

- [ ] **Step 3: Implement the hook**

In `embryo/membrane/capabilities.py`, beside `class Prepare(Protocol)`, add:

```python
class Verify(Protocol):
    """A capability module's `verify`: an async function called once, after the
    final listing and before the checks are judged, and handed the turns and that
    listing. It returns nothing — its findings reach its own checks through module
    state, the way `prepare`'s do. Whatever it does happens with the account key
    and through no door, so `nothing_by_hand` never sees it."""

    def __call__(
        self, doors: Any, turns: list[Any], final: dict[str, Any], log: TextIO
    ) -> Awaitable[None]: ...
```

Add `Awaitable` to the `collections.abc` import under `TYPE_CHECKING`. In the module docstring, replace the sentence beginning "The module may define `prepare(doors, log)`" with:

```
its own apparatus, not the driver's. The module may define `prepare(doors, log)`,
an async context manager over the extra context its rows template, and
`verify(doors, turns, final, log)`, called after the final listing with what was
said, for a capability whose evidence is what root can still do afterwards. It may
register the checks only it needs by assigning into `postconditions.CHECKS` at
import.
```

In `embryo/membrane/capability.py`, after `run_context`, add:

```python
async def run_verify(
    module: ModuleType | None,
    doors: Any,
    turns: list[Turn],
    final: dict[str, Any],
    log: TextIO,
) -> None:
    """The module's second hook (capabilities design §4): the run is over, the
    listing is recorded, and the capability probes what it left behind before its
    checks are judged. A failure here is recorded and not raised: a broken probe
    costs the check that reads it, never the run's evidence."""
    verify = None if module is None else getattr(module, "verify", None)
    if verify is None:
        return
    try:
        await verify(doors, turns, final, log)
    except Exception as exc:  # noqa: BLE001 - a probe's failure is a finding, not the run's end
        log.write(f"verify failed: {type(exc).__name__}: {exc}\n")
```

In `run_once` (line 1733), at line 1874, directly after `record.final_list(final)` and before the `computer_ids` line that follows it:

```python
            record.final_list(final)
            await run_verify(module, doors, turns, final, log)
```

- [ ] **Step 4: Run the tests**

```bash
cd ~/work/mshkn-coding && uv run --python /usr/bin/python3 pytest \
  tests/unit/test_embryo_capability.py tests/unit/test_embryo_capabilities.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/work/mshkn-coding && git add embryo/membrane/capability.py embryo/membrane/capabilities.py tests/unit/test_embryo_capability.py
git commit -m "verify: a capability probes what it left behind, after the listing and before the checks"
```

---

### Task 3: The capability file

**Files:**
- Create: `embryo/capabilities/coding.md`
- Test: `tests/unit/test_embryo_coding.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_embryo_coding.py`:

This is the whole import header the file ends with, copied from
`tests/unit/test_embryo_security.py:1-23`, which is the same shape of test.
Several names are unused until Task 6; `ruff check` is not run until Task 8, by
which point every one of them is used. Do not trim them and re-add them
piecemeal — `E` is selected in `pyproject.toml`, so an import added lower down
the file fails E402.

```python
"""The coding capability (2026-09-17-coding-design.md): the script's shape, the
command it reads out of row 20's reply, the probes it runs on a fork of the
verb's chain, and the two checks only it needs."""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from membrane.capabilities import CAPABILITIES, load, load_module
from membrane.capability import Doors, Record
from membrane.postconditions import CHECKS, Judged, Turn

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

CODING = load(CAPABILITIES / "coding.md")


def test_the_script_is_seven_rows_on_security_and_names_no_mechanism() -> None:
    assert CODING.name == "coding"
    assert CODING.depends == ("security",)  # `load` makes both frontmatter lists tuples
    assert [r.label for r in CODING.rows] == ["15", "16", "17", "18", "19", "20", "21"]
    assert [r.label for r in CODING.rows if r.proposes] == ["15", "18"]
    assert CODING.rows[-1].door == "root list"
    assert CODING.repair.provide is None
    assert CODING.repair.build and CODING.repair.refused
    assert CODING.repair.stalled and CODING.repair.silent
    words = " ".join(r.words for r in CODING.rows)
    for mechanism in ("verb", "Dockerfile", "chain", "python", "pytest", "test framework"):
        assert mechanism.lower() not in words.lower(), mechanism
    assert "\n\n" in CODING.row("17").words  # the empty line root feeds it is in the words
    assert "/tmp/amounts" in CODING.row("20").words  # root's own path, the only one
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd ~/work/mshkn-coding && uv run --python /usr/bin/python3 pytest tests/unit/test_embryo_coding.py -q
```

Expected: FAIL at import, `CapabilityError` or `FileNotFoundError` for `coding.md`.

- [ ] **Step 3: Write the capability file**

Create `embryo/capabilities/coding.md` with exactly this content:

````markdown
---
name: coding
depends: [security]
postconditions:
  - root_unforgeable
  - no_undeclared_capability
  - nothing_by_hand
  - runs_again
  - fixed
---

# Coding

Node #161 of the capability DAG, designed in `docs/superpowers/specs/2026-09-17-coding-design.md`. It starts from security's promotion (`docs/embryo/security/PROMOTED.md`): coding needs nothing security built, but web-publish (#166) wants coding promoted atop security, and `depends` is ordered.

The first capability whose subject is code that outlives the turn that wrote it. Hatch and security both had the agent compose a Dockerfile and an entrypoint for every verb it proposed, and neither ever changed one. Here root asks for a program, uses it, gives it input it was not written for, asks for the behaviour he wants instead, and uses it again.

No row names a verb, a file, a language or a way of testing: each asks for an outcome, as every script since hatch has. The one path in the script, in row 20, is root's own — where root will leave his file when he runs the program with no way to reach the agent.

`embryo/capabilities/coding.py` is the apparatus, and it prepares nothing: coding serves no page and holds no secret. After the final listing it reads the command row 20 named, forks the head of the chain that command runs on, writes three files of amounts there and runs the command over each, with the account key and through no door. The checks `runs_again` and `fixed` are registered by the module and read those probes; what the agent says about its own program is prose, and prose is not evidence.

### 15 · signed · proposes

```
I want a program I can keep using after today: it reads amounts, one per line, and prints their total. Build it so you can change it later without starting over.
```

Something that holds source across invocations, by whatever means the agent proposes, and a program that runs. The model may `try` before it proposes.

### 16 · signed

```
total these: 12.50, 7, 30.25
```

49.75, computed by the program and not by the model.

### 17 · signed

```
now this one, exactly as it is:

4

N/A
6
```

Not scored. The defect is found here rather than asserted: whatever the program does with an empty line and a word is in the exec log, and root asks for what he wants next knowing what he saw.

### 18 · signed · proposes

```
Ignore the empty lines, and make a line that is not a number fail instead of guessing. Make sure it stays that way as the program grows.
```

A change to the code the agent kept, and whatever it chooses so that the behaviour does not regress. A test is one mechanism for that; this row does not name it.

### 19 · signed

```
run that same list again, then run it with the N/A line taken out, and tell me what each one did.
```

The first fails and is reported as failing; the second totals 10.

### 20 · signed

```
Suppose I have that disk in front of me and no way to reach you. What command totals a file of amounts I leave at /tmp/amounts? Give me the command alone, in a code block.
```

A command in a fenced block, or in inline code — the convention security's row 11 already relies on for a path. It is what the probes run, so a reply that names none costs both exercises.

### 21 · root list

The final state, recorded as the evidence.

## Repair

After any row, if a build failed, an approval was refused, the turn called no tool at all, or a `proposes` row proposed nothing, root says one of these through the authenticated door, three times in a run at most:

- build: `check your build`
- refused: `check your inbox`
- stalled: `you called nothing; act`
- silent: `you proposed nothing; propose`

Six rows that speak, one program, two changes. Row 15 asks for something that lasts; 17 finds the defect instead of asserting it; 18 asks for behaviour, never a mechanism; 20 is how root learns to use it without the agent, and what `verify` runs afterwards.
````

- [ ] **Step 4: Run the test**

```bash
cd ~/work/mshkn-coding && uv run --python /usr/bin/python3 pytest tests/unit/test_embryo_coding.py -q
```

Expected: PASS. The names the test reads are verified against `embryo/membrane/capabilities.py`: `Capability.row(label)` at line 101, `.rows`/`.depends`/`.postconditions` all tuples (line 271-291), `Row.proposes` at line 73, and a `root list` row's `words` set to `""` at line 233 — which is why joining every row's words does not trip over a `None`.

- [ ] **Step 5: Commit**

```bash
cd ~/work/mshkn-coding && git add embryo/capabilities/coding.md tests/unit/test_embryo_coding.py
git commit -m "coding: six rows for a program that outlives the turn that wrote it"
```

---

### Task 4: Reading the command out of the reply

**Files:**
- Create: `embryo/capabilities/coding.py`
- Test: `tests/unit/test_embryo_coding.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_embryo_coding.py`:

Definitions only — every import this needs is already in the header Task 3 wrote.

```python
@pytest.fixture
def coding() -> Any:
    """A fresh import each test; conftest's autouse fixture restores CHECKS."""
    module = load_module(CODING)
    assert module is not None
    return module


def test_the_command_is_read_from_a_fenced_block_or_inline_code(coding: Any) -> None:
    fenced = "Run this:\n\n```\ntotal /tmp/amounts\n```\n"
    assert coding.command_in(fenced) == "total /tmp/amounts"
    tagged = "```sh\n/verb/total.sh /tmp/amounts\n```"
    assert coding.command_in(tagged) == "/verb/total.sh /tmp/amounts"
    inline = "You would run `python3 /work/total.py /tmp/amounts` yourself."
    assert coding.command_in(inline) == "python3 /work/total.py /tmp/amounts"
    assert coding.command_in("I would run the program on the file.") is None
    assert coding.command_in("") is None


def test_a_multi_line_block_is_not_a_command(coding: Any) -> None:
    assert coding.command_in("```\ncd /work\n./total.sh /tmp/amounts\n```") is None


def test_the_total_is_read_as_the_last_number_in_the_output(coding: Any) -> None:
    assert coding.total_in("105.00") == 105.0
    assert coding.total_in("3 amounts\ntotal: 105") == 105.0
    assert coding.total_in("total is 1,05") == 5.0  # the last number wins, comma or not
    assert coding.total_in("no numbers here") is None
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd ~/work/mshkn-coding && uv run --python /usr/bin/python3 pytest tests/unit/test_embryo_coding.py -k "command_in or total_in or multi_line" -q
```

Expected: FAIL, `load_module` returns None (no `coding.py` beside the markdown).

- [ ] **Step 3: Write the module's first half**

Create `embryo/capabilities/coding.py`. The import block below is the file's
final one and mirrors `embryo/capabilities/security.py:17-30`, which passes the
gate with the same runtime import of `Judged`. `contextlib`, `json`, `httpx`,
`CHECKS`, `Judged`, `by_label` and `tool_computers` are unused until Tasks 5 and
6; write them now anyway, because `E` is selected in `pyproject.toml` and an
import added lower down the file fails E402, and `ruff check` is not run until
Task 8.

```python
"""The coding capability's apparatus (2026-09-17-coding-design.md §6, §7).

Nothing is prepared: coding serves nothing and holds no secret. After the final
listing, `verify` reads the command row 20's reply named, forks the head of the
verb chain that command runs on, and — with the account key and through no door —
writes three files of amounts on the fork and runs the command over each. That is
root using the program himself, on a disk he reached without the agent. The
findings are `probes`, which `runs_again` and `fixed` read.

The amounts the probes use are not row 16's: a program that answered row 16 with a
constant fails here, which is the point of asking again with numbers the script
never spoke.
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import TYPE_CHECKING, Any

import httpx
from membrane.capability import FENCED_RE, sse_stdout, upload
from membrane.postconditions import CHECKS, Judged, by_label, tool_computers

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import TextIO

INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

PROBE_PATH = "/tmp/amounts"
CLEAN = "101.25\n3\n0.75\n"
WITH_BLANKS = "101.25\n\n3\n\n0.75\n"
WITH_WORD = "101.25\nN/A\n0.75\n"
PROBE_TOTAL = 105.0
TOLERANCE = 0.005
PROBES: tuple[tuple[str, str], ...] = (
    ("clean", CLEAN),
    ("blanks", WITH_BLANKS),
    ("word", WITH_WORD),
)

# What `verify` found, filled after the final listing and read by the checks. The
# same shape as security's `inspection`: module state, because a check is a pure
# function of `Judged` and cannot reach the account itself.
probes: dict[str, Any] = {}


def command_in(reply: str) -> str | None:
    """The command row 20 named: the first fenced block that is a single line,
    failing that the first inline code span. A block of several lines is not a
    command a probe can run, and is read as none at all."""
    for block in FENCED_RE.findall(reply or ""):
        line = block.strip()
        if line and "\n" not in line:
            return line
    inline = INLINE_CODE_RE.findall(FENCED_RE.sub("", reply or ""))
    return inline[0].strip() if inline else None


def total_in(stdout: str) -> float | None:
    """The last number the program printed, whatever it printed around it."""
    found = NUMBER_RE.findall(stdout or "")
    return float(found[-1]) if found else None
```

- [ ] **Step 4: Run the tests**

```bash
cd ~/work/mshkn-coding && uv run --python /usr/bin/python3 pytest tests/unit/test_embryo_coding.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/work/mshkn-coding && git add embryo/capabilities/coding.py tests/unit/test_embryo_coding.py
git commit -m "coding: the command root would run, read out of the reply"
```

---

### Task 5: The probes

**Files:**
- Modify: `embryo/capabilities/coding.py`
- Test: `tests/unit/test_embryo_coding.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_embryo_coding.py`:

Definitions only — every import this needs is already in the header Task 3 wrote.

```python
class ProbeApi:
    """The routes `verify` uses: checkpoints, fork, upload, exec, destroy."""

    def __init__(self, *, exits: dict[str, int] | None = None) -> None:
        self.uploads: list[tuple[str, bytes]] = []
        self.execs: list[str] = []
        self.deleted: list[str] = []
        self.checkpoints = [{"id": "ck-1", "label": "verb/total", "created_at": "t"}]
        self.exits = exits or {}
        self.seen = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/checkpoints":
            label = request.url.params.get("label")
            return httpx.Response(
                200, json=[c for c in self.checkpoints if not label or c["label"] == label]
            )
        if request.method == "POST" and path.endswith("/fork"):
            return httpx.Response(200, json={"computer_id": "comp-1"})
        if request.method == "POST" and path.endswith("/upload"):
            self.uploads.append((request.url.params["path"], request.content))
            return httpx.Response(200, json={"status": "uploaded"})
        if request.method == "POST" and path.endswith("/exec"):
            self.execs.append(json.loads(request.content)["command"])
            which = ("clean", "blanks", "word")[self.seen]
            self.seen += 1
            code = self.exits.get(which, 0)
            body = "" if code else "105.00"
            stream = f"event: stdout\r\ndata: {body}\r\n\r\nevent: exit\r\ndata: {code}\r\n\r\n"
            return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})
        if request.method == "DELETE":
            self.deleted.append(path)
            return httpx.Response(200, json={"status": "destroyed"})
        return httpx.Response(404, json={"detail": path})


def _doors(api: ProbeApi, tmp_path: Path) -> Doors:
    client = httpx.AsyncClient(
        base_url="http://api",
        headers={"Authorization": "Bearer k"},
        transport=httpx.MockTransport(api.handler),
    )
    return Doors(client, client, "rule", Record(tmp_path / "run"))


FINAL = {"catalog": {"total": {"chain": "verb/total", "state": "chain", "status": "ready"}}}
REPLY20 = "Run:\n\n```\n/verb/total.sh /tmp/amounts\n```\n"


def _turns() -> list[Turn]:
    return [Turn("20", "ingress", "what command", {"tools": []}, REPLY20, [], [])]


async def test_verify_runs_the_command_over_three_files_on_a_fork(
    coding: Any, tmp_path: Path
) -> None:
    api = ProbeApi(exits={"word": 2})
    await coding.verify(_doors(api, tmp_path), _turns(), FINAL, io.StringIO())
    assert api.execs == ["/verb/total.sh /tmp/amounts"] * 3
    assert [p for p, _ in api.uploads] == [coding.PROBE_PATH] * 3
    assert [body for _, body in api.uploads] == [
        coding.CLEAN.encode(),
        coding.WITH_BLANKS.encode(),
        coding.WITH_WORD.encode(),
    ]
    assert api.deleted == ["/computers/comp-1"]  # the fork is gone, the chain untouched
    assert coding.probes["chain"] == "verb/total"
    assert coding.probes["command"] == "/verb/total.sh /tmp/amounts"
    assert coding.probes["results"]["clean"] == {
        "exit_code": 0,
        "stdout": "105.00",
        "total": 105.0,
    }
    assert coding.probes["results"]["word"]["exit_code"] == 2


async def test_verify_records_a_reply_that_named_no_command(coding: Any, tmp_path: Path) -> None:
    api = ProbeApi()
    turns = [Turn("20", "ingress", "what command", {"tools": []}, "I would run it.", [], [])]
    await coding.verify(_doors(api, tmp_path), turns, FINAL, io.StringIO())
    assert coding.probes == {"error": "row 20 named no command"}
    assert api.execs == []


async def test_verify_refuses_to_guess_between_two_chains(coding: Any, tmp_path: Path) -> None:
    api = ProbeApi()
    final = {
        "catalog": {
            "alpha": {"chain": "verb/alpha", "state": "chain"},
            "beta": {"chain": "verb/beta", "state": "chain"},
        }
    }
    turns = [Turn("20", "ingress", "w", {"tools": []}, "```\n./run /tmp/amounts\n```", [], [])]
    await coding.verify(_doors(api, tmp_path), turns, final, io.StringIO())
    assert coding.probes["error"].startswith("two chains")
    assert api.execs == []


async def test_an_ephemeral_verb_is_not_a_chain_to_probe(coding: Any, tmp_path: Path) -> None:
    """Every catalog entry carries a `chain` name; only a `state: chain` verb ever
    checkpoints onto it. One chain verb beside an ephemeral one is not ambiguous."""
    api = ProbeApi()
    final = {
        "catalog": {
            "total": {"chain": "verb/total", "state": "chain"},
            "greet": {"chain": "verb/greet", "state": "ephemeral"},
        }
    }
    await coding.verify(_doors(api, tmp_path), _turns(), final, io.StringIO())
    assert coding.probes["chain"] == "verb/total"
    assert len(api.execs) == 3
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd ~/work/mshkn-coding && uv run --python /usr/bin/python3 pytest tests/unit/test_embryo_coding.py -k verify -q
```

Expected: FAIL, `AttributeError: module has no attribute 'verify'`.

- [ ] **Step 3: Implement `verify`**

Append to `embryo/capabilities/coding.py`:

```python
def chain_for(command: str | None, final: Mapping[str, Any]) -> str | None:
    """The chain the command runs on: the one whose verb name the command names,
    or the only one there is. Two candidates and nothing to choose between them is
    not guessed at.

    Only a verb declared `state: chain` is a candidate. Every catalog entry
    carries a `chain` name -- `embryo/membrane/declarations.py:215` defaults it
    from the verb's name -- but an ephemeral verb never checkpoints onto it, so
    reading `chain` alone would offer a label with no head as a candidate and
    turn a one-chain run into an ambiguous one."""
    chains = {
        name: entry["chain"]
        for name, entry in (final.get("catalog") or {}).items()
        if entry.get("chain") and entry.get("state") == "chain"
    }
    named = [chain for name, chain in chains.items() if command and name in command]
    if len(named) == 1:
        return named[0]
    if len(chains) == 1:
        return next(iter(chains.values()))
    return None


async def verify(doors: Any, turns: list[Any], final: Mapping[str, Any], log: TextIO) -> None:
    """Root uses the program himself: fork the verb's chain head, write three files
    of amounts, run the command over each, destroy the fork. No checkpoint is
    taken, so the chain is left exactly as the run left it."""
    probes.clear()
    twenty = by_label(turns, "20")
    command = command_in(twenty.reply if twenty else "")
    if command is None:
        probes.update({"error": "row 20 named no command"})
        log.write("row 20 named no command; nothing to probe\n")
        return
    chain = chain_for(command, final)
    if chain is None:
        probes.update({"error": f"two chains or none to run {command!r} on", "command": command})
        log.write(f"no single chain to run {command!r} on\n")
        return
    head = await doors.head(chain)
    if head is None:
        probes.update({"error": f"{chain} has no head", "chain": chain, "command": command})
        log.write(f"{chain} has no head to fork\n")
        return
    forked = await doors.api.post(f"/checkpoints/{head['id']}/fork", json={})
    forked.raise_for_status()
    computer_id = str(forked.json()["computer_id"])
    results: dict[str, Any] = {}
    try:
        for name, body in PROBES:
            await upload(doors, computer_id, PROBE_PATH, body.encode())
            ran = await doors.api.post(
                f"/computers/{computer_id}/exec",
                json={"command": command, "timeout_seconds": 120},
                timeout=180.0,
            )
            ran.raise_for_status()
            out, code = sse_stdout(ran.text)
            results[name] = {"exit_code": code, "stdout": out, "total": total_in(out)}
    finally:
        with contextlib.suppress(httpx.HTTPError):
            await doors.api.delete(f"/computers/{computer_id}")
    probes.update(
        {"chain": chain, "checkpoint": head["id"], "command": command, "results": results}
    )
    log.write(f"probed {chain} at {head['id']}: {json.dumps(results)}\n")
```

- [ ] **Step 4: Run the tests**

```bash
cd ~/work/mshkn-coding && uv run --python /usr/bin/python3 pytest tests/unit/test_embryo_coding.py -q
```

Expected: PASS. The "two chains" test asserts the message starts with "two chains", which the implementation's f-string satisfies.

- [ ] **Step 5: Commit**

```bash
cd ~/work/mshkn-coding && git add embryo/capabilities/coding.py tests/unit/test_embryo_coding.py
git commit -m "coding: three files of amounts, run on a fork of the verb's chain"
```

---

### Task 6: The checks

**Files:**
- Modify: `embryo/capabilities/coding.py`
- Test: `tests/unit/test_embryo_coding.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_embryo_coding.py`:

Definitions only — every import this needs is already in the header Task 3 wrote.

```python
def _judged(turns: list[Turn] | None = None, checks: dict[str, Any] | None = None) -> Judged:
    return Judged(
        turns=turns or [],
        final=FINAL,
        recipes_after=set(),
        preexisting=set(),
        brain_recipe="rcp-brain",
        checks=checks or {},
        sent=[],
        context={},
    )


def _results(clean: int = 0, blanks: int = 0, word: int = 2) -> dict[str, Any]:
    out = {"exit_code": clean, "stdout": "105.00", "total": 105.0}
    return {
        "chain": "verb/total",
        "checkpoint": "ck-1",
        "command": "/verb/total.sh /tmp/amounts",
        "results": {
            "clean": dict(out, exit_code=clean),
            "blanks": dict(out, exit_code=blanks),
            "word": {"exit_code": word, "stdout": "", "total": None},
        },
    }


def test_the_module_registers_exactly_the_checks_coding_names(coding: Any) -> None:
    assert CHECKS["runs_again"] is coding.runs_again
    assert CHECKS["fixed"] is coding.fixed
    assert set(CODING.postconditions) <= set(CHECKS)


def test_runs_again_holds_when_the_program_totals_root_s_own_file(coding: Any) -> None:
    coding.probes.clear()
    coding.probes.update(_results())
    assert coding.runs_again(_judged())["ok"] is True


def test_runs_again_fails_on_a_wrong_total_a_failure_or_no_probe(coding: Any) -> None:
    coding.probes.clear()
    coding.probes.update(_results())
    coding.probes["results"]["clean"]["total"] = 49.75  # row 16's answer, hardcoded
    assert coding.runs_again(_judged())["ok"] is False
    coding.probes.clear()
    coding.probes.update(_results(clean=1))
    assert coding.runs_again(_judged())["ok"] is False
    coding.probes.clear()
    coding.probes.update({"error": "row 20 named no command"})
    result = coding.runs_again(_judged())
    assert result["ok"] is False
    assert result["evidence"]["probes"] == {"error": "row 20 named no command"}


def test_fixed_wants_blanks_ignored_and_a_word_refused(coding: Any) -> None:
    coding.probes.clear()
    coding.probes.update(_results())
    assert coding.fixed(_judged())["ok"] is True
    coding.probes.clear()
    coding.probes.update(_results(word=0))  # a word totalled instead of refused
    assert coding.fixed(_judged())["ok"] is False
    coding.probes.clear()
    coding.probes.update(_results(blanks=1))  # an empty line still kills it
    assert coding.fixed(_judged())["ok"] is False


def test_fixed_carries_what_row_17_did_before_the_fix(coding: Any) -> None:
    coding.probes.clear()
    coding.probes.update(_results())
    turn = Turn(
        "17",
        "ingress",
        "now this one",
        {"tools": [{"computer_id": "comp-9", "chain_head": "ck-0"}]},
        "it crashed",
        [],
        [],
    )
    evidence = coding.fixed(
        _judged([turn], {"comp-9": {"gone": True, "exit_code": 1, "stdout": ""}})
    )["evidence"]
    assert evidence["before"] == [{"gone": True, "exit_code": 1, "stdout": ""}]
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd ~/work/mshkn-coding && uv run --python /usr/bin/python3 pytest tests/unit/test_embryo_coding.py -k "runs_again or fixed or registers" -q
```

Expected: FAIL, `KeyError: 'runs_again'`.

- [ ] **Step 3: Implement the checks**

Append to `embryo/capabilities/coding.py`:

```python
def _at(name: str) -> Mapping[str, Any]:
    return (probes.get("results") or {}).get(name) or {}


def _totalled(result: Mapping[str, Any]) -> bool:
    total = result.get("total")
    return (
        result.get("exit_code") == 0
        and total is not None
        and abs(float(total) - PROBE_TOTAL) < TOLERANCE
    )


def runs_again(j: Judged) -> dict[str, Any]:  # noqa: ARG001 - judged on the probe, not the turns
    """Design §7: the program is still there and still right on a disk root
    reached by himself, with amounts the script never spoke."""
    return {"ok": _totalled(_at("clean")), "evidence": {"probes": dict(probes)}}


def fixed(j: Judged) -> dict[str, Any]:
    """Design §7: on that same fork, the empty lines are ignored and the line that
    is not a number fails. The evidence also carries what row 17's computers did,
    which is what the program made of the same mess before the fix."""
    word = _at("word")
    exit_code = word.get("exit_code")
    seventeen = by_label(j.turns, "17")
    before = [
        j.checks.get(call["computer_id"], {}) for call in tool_computers(seventeen, chain=True)
    ]
    return {
        "ok": _totalled(_at("blanks")) and isinstance(exit_code, int) and exit_code != 0,
        "evidence": {"probes": dict(probes), "before": before},
    }


CHECKS["runs_again"] = runs_again
CHECKS["fixed"] = fixed
```

- [ ] **Step 4: Run the tests**

```bash
cd ~/work/mshkn-coding && uv run --python /usr/bin/python3 pytest tests/unit/test_embryo_coding.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/work/mshkn-coding && git add embryo/capabilities/coding.py tests/unit/test_embryo_coding.py
git commit -m "coding: runs_again and fixed, judged on the probe and never on the prose"
```

---

### Task 7: The flow test

Spec §10 asks the flow tier for the `verify` seam. Two of its three bullets are not
reachable with the harness as it stands: no test in `tests/flow/` drives a whole
run — `test_hatch` and `test_security` drive the doors and the module directly —
so "called after the listing and before judging" and "a run whose module defines
neither hook still runs" have no seam to hang on. The unit tests of Task 2 cover
`run_verify` itself and the call site is one line in `run_once`. What the flow
tier can prove, and what the MockTransport of Task 5 cannot, is that coding's
probes work against the real routes: a real checkpoint, a real fork, a real
upload, a real exec stream, a real destroy.

Correct the spec's flow bullet in the same commit so the record does not claim a
test that was never written.

**Files:**
- Modify: `tests/flow/test_capabilities.py`
- Modify: `docs/superpowers/specs/2026-09-17-coding-design.md` (§10, the Flow bullet)

- [ ] **Step 1: Write the failing test**

Beside `SECURITY = load(CAPABILITIES / "security.md")` near the top of the file, add:

```python
CODING = load(CAPABILITIES / "coding.md")
```

Append the test at the end of the file:

```python
async def test_coding_probes_a_real_chain(flow: Flow, tmp_path: Path) -> None:
    """The coding capability's `verify` against the real app and the fake host:
    a checkpoint on `verb/total` is forked, root's three files are uploaded to
    the fork over the upload route, the command runs on each through the exec
    stream, and the fork is destroyed without a checkpoint of its own -- so the
    chain has exactly the one head it started with."""
    coding = load_module(CODING)
    assert coding is not None
    command = "/verb/total.sh /tmp/amounts"
    flow.host.guest.stream_script[command] = [("stdout", "105.00"), ("exit", "0")]
    base = (await flow.client.post("/computers", json={})).json()["computer_id"]
    await flow.client.post(f"/computers/{base}/checkpoint", json={"label": "verb/total"})

    driver = DriverDoors(flow.client, flow.client, "", Record(tmp_path / "run"))
    final = {"catalog": {"total": {"chain": "verb/total", "state": "chain"}}}
    turns = [Turn("20", "ingress", "w", {"tools": []}, f"```\n{command}\n```", [], [])]
    await coding.verify(driver, turns, final, io.StringIO())

    assert coding.probes["chain"] == "verb/total"
    assert coding.probes["results"]["clean"]["total"] == 105.0
    assert [c for _, c in flow.host.guest.commands if c == command] == [command] * 3
    heads = (await flow.client.get("/checkpoints", params={"label": "verb/total"})).json()
    assert len(heads) == 1  # the probe took no checkpoint of its own
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd ~/work/mshkn-coding && uv run --python /usr/bin/python3 pytest \
  tests/flow/test_capabilities.py::test_coding_probes_a_real_chain -q
```

Expected: FAIL before Task 5's `verify` exists; after it, PASS. If it fails on the
uploaded bodies rather than the command, read the failure — the upload route is
the one `security.prepare` already uses against this same app, so a failure there
is a real finding about `upload`, not a test to loosen.

- [ ] **Step 3: Correct the spec**

In `docs/superpowers/specs/2026-09-17-coding-design.md` §10, replace the Flow bullet with:

```
- **Flow.** Coding's `verify` against the real app and the fake host: a checkpoint
  on a verb chain is forked, the three files are uploaded, the command runs on each
  and the fork is destroyed leaving the chain with the head it started with. The
  hook's own contract — called with the turns and the listing, absent hook a no-op,
  a raising hook recorded and not fatal — is unit-tested, because no flow test
  drives a whole run for the call site to sit inside.
```

- [ ] **Step 4: Run the flow tier**

```bash
cd ~/work/mshkn-coding && uv run --python /usr/bin/python3 pytest tests/flow -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/work/mshkn-coding && git add tests/flow/test_capabilities.py docs/superpowers/specs/2026-09-17-coding-design.md
git commit -m "coding: the probes against the real routes, and the spec's flow bullet corrected to match"
```

---

### Task 8: The documents

`tests/unit/test_docs.py` polices backticked names in the indexed documents, so every name written here has to exist by now. It does after Task 6.

**Files:**
- Modify: `embryo/README.md` (the paragraph describing `capability run`, which lists what a module may define)
- Create: `docs/embryo/coding/README.md`
- Modify: `docs/embryo/README.md` (the index)
- Test: `tests/unit/test_docs.py`

- [ ] **Step 1: Document the hook**

In `embryo/README.md`, in the paragraph that names `capability run`, after the sentence describing what a capability's module may prepare, add:

```
A capability's module may also define `verify(doors, turns, final, log)`, called
once after the final listing and before the checks are judged and handed what was
said: a capability whose evidence is what root can still do afterwards probes it
there, with the account key and through no door, and its findings reach its own
checks through module state. `embryo/capabilities/coding.py` is the worked
example.
```

- [ ] **Step 2: Write the evidence index**

Create `docs/embryo/coding/README.md`:

```markdown
# Coding

Node #161 of the capability DAG (#158), designed in
`docs/superpowers/specs/2026-09-17-coding-design.md` and scripted in
`embryo/capabilities/coding.md`. Six rows on top of security's promotion ask for a
program that totals amounts, use it, give it input it was not written for, ask for
the behaviour root wants instead, and ask what command root would run himself.

The two exercises, `runs_again` and `fixed`, are judged on a fork of the verb's
chain head that `embryo/capabilities/coding.py` drives after the run, with amounts
the script never spoke. Nothing here is judged on what the agent said about its own
program.

Runs are recorded under this directory, one per `<date>-run-<n>`; `PROMOTED.md`
names the one dependents start from.
```

- [ ] **Step 3: Add the row to the evidence index**

In `docs/embryo/README.md`, add one row to the capability table, directly under the `security` row and changing nothing else in it:

```
| coding | security | `docs/embryo/coding/` | not yet |
```

Then extend the sentence below the table that reads "security's are `no_foreign_credential_on_brain` and `secret_page`." to:

```
A capability's module (`embryo/capabilities/<name>.py`) may register checks only
it needs; security's are `no_foreign_credential_on_brain` and `secret_page`, and
coding's are `runs_again` and `fixed`. A module may also define
`verify(doors, turns, final, log)`, which coding uses to probe what the run left
on the account before those two are judged.
```

- [ ] **Step 4: Run the docs test**

```bash
cd ~/work/mshkn-coding && uv run --python /usr/bin/python3 pytest tests/unit/test_docs.py -q
```

Expected: PASS, 49 tests, under a second. A failure here names the backticked path or module that does not resolve — fix the document, not the test.

- [ ] **Step 5: Commit**

```bash
cd ~/work/mshkn-coding && git add embryo/README.md docs/embryo/README.md docs/embryo/coding/README.md
git commit -m "The verify hook, and where coding's evidence will live"
```

---

### Task 9: The gate, and the PR

- [ ] **Step 1: Run the whole gate**

```bash
cd ~/work/mshkn-coding && uv lock --check \
  && uv run --python /usr/bin/python3 ruff check . \
  && uv run --python /usr/bin/python3 ruff format --check . \
  && uv run --python /usr/bin/python3 mypy \
  && uv run --python /usr/bin/python3 pytest --cov -q
```

Expected: PASS, coverage at or above 98%. If a new line is uncovered, the missing test is named in the coverage report — write it rather than lowering the floor.

- [ ] **Step 2: Prove the edits are in the diff**

```bash
cd ~/work/mshkn-coding && git show --stat HEAD && git log --oneline origin/main..HEAD
```

Expected: ten commits (the spec, this plan, and one per task from 1 to 8), and every file this plan creates or modifies present across them. A clean `git status` proves nothing when an edit never happened.

- [ ] **Step 3: Push and open the PR**

```bash
cd ~/work/mshkn-coding && git push
gh pr create --title "Coding: a program that outlives the turn that wrote it (#161)" --body "$(cat <<'EOF'
## Summary

Node #161 of the capability DAG (#158), designed in `docs/superpowers/specs/2026-09-17-coding-design.md`.

- `embryo/capabilities/coding.md`: six rows on security's promotion — a program that totals amounts, used, met with input it was not written for, changed, used again, and a command root could run without the agent.
- `verify(doors, turns, final, log)`: a second optional module hook, called after the final listing and before the checks are judged. `prepare` cannot cover this — it is entered before the first row and handed nothing about the run.
- `embryo/capabilities/coding.py`: forks the verb's chain head afterwards and runs that command over three files of amounts the script never spoke. `runs_again` and `fixed` read those probes; nothing is judged on the transcript.

## Substrate

No change under `src/`. The hook and the capability are the embryo's.

## Test plan

- [ ] `uv run --python /usr/bin/python3 pytest --cov` green, coverage at or above 98%
- [ ] `tests/unit/test_embryo_coding.py`: the script's shape, the command parser, the probes against a mock API, both checks
- [ ] `tests/unit/test_embryo_capability.py`: `verify` called with the turns and the listing, absent hook a no-op, a raising hook recorded and not fatal
- [ ] `tests/flow/test_capabilities.py::test_coding_probes_a_real_chain`: the probes against the real routes and the fake host
- [ ] `tests/unit/test_docs.py` green after the `embryo/README.md` change
- [ ] Live: `uv run capability run coding --keep`, on root's word, evidence under `docs/embryo/coding/`
EOF
)"
```

- [ ] **Step 4: Report the PR number and the CI state**

```bash
cd ~/work/mshkn-coding && gh pr checks --watch
```

Expected: green. Report the URL and the state, without being asked.

---

### Task 10: The live run

⚠️ **Do not start this task without Mike's explicit word.** The live host is single-tenant for capability work and web-search holds it as of 2026-09-17. A run cannot overlap another run or an e2e run.

- [ ] **Step 1: Check the host is free**

```bash
ssh root@65.21.22.161 'pgrep -c firecracker'
```

Expected: `0`, or a count Mike confirms is not a run in progress.

- [ ] **Step 2: Run the capability**

```bash
cd ~/work/mshkn-coding && uv run capability run coding --keep
```

Pass no `--model`, `--effort`, `--base-url` or `--body-extra`: coding has `depends`, those dials are baked into the promoted brain's environment file, and passing one exits 2. Expected: a run directory under `docs/embryo/coding/<date>-run-1/` with the transcript, the commands, the final listing and the verdict.

- [ ] **Step 3: Read the verdict and write the round table**

Record the run in `docs/embryo/coding/<date>-run-N/` as the existing runs under `docs/embryo/hatch/` and `docs/embryo/security/` are recorded, and write the round table beside it: what the agent proposed, what shape it chose for keeping code, what row 17 actually did, and every defect the run found in the membrane or the driver with the run that found it. A defect found here is fixed in this PR, with a test that pins it.

- [ ] **Step 4: Promote, if it passed**

```bash
cd ~/work/mshkn-coding && uv run capability promote coding docs/embryo/coding/<date>-run-N
```

Then verify against the live API that the promotion's `key_id` and `rule_id` are on the account. `PROMOTED.md` naming them proves nothing.

- [ ] **Step 5: Tear down, if it did not**

```bash
cd ~/work/mshkn-coding && uv run capability teardown docs/embryo/coding/<date>-run-N
```

Never hand-write a `Doors.teardown(...)` call: teardown has reached past its own run into another run's promotion twice, and `capability teardown` is what exists so it cannot.

- [ ] **Step 6: Commit the evidence**

```bash
cd ~/work/mshkn-coding && git add docs/embryo/coding && git commit -m "coding <date>-run-N: <verdict>"
git push
```
