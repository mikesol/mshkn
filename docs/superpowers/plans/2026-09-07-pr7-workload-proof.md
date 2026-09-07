# PR 7: The Workload Proof — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove on the live host the loop a generative agent lives in (write a recipe, build it, read the failure, fix it, run real tools, checkpoint, fork, diverge, pick) with eight Phase 10 tests and T7.9, and ship the four product changes the proof cannot be written without: the base-image rule (#73), a caller-chosen exec timeout, an honest exit code after a kill, and recipe image retention.

**Architecture:** Product changes are small and local: a Dockerfile parser and an `InvalidInput` in `RecipeService.create`; a `timeout_seconds` field threaded from `ExecRequest` through `ComputerService.stream` to `Guest.stream`; a three-branch exit-code function in `SshGuest`; the recipe build keeps its image and the delete removes it. The E2E conftest gains `timeout_seconds` on `exec_command`, `wait_for_recipe`, `upload_file` and `port_url`; `tests/e2e/test_phase10_integration.py` is rewritten; `test_phase7_api.py` gains T7.9; `test_phase3_capabilities.py`'s bad-Dockerfile test fails during the build instead of at validation.

**Tech Stack:** Python 3.12, FastAPI, pydantic, asyncssh, aiosqlite, Docker (legacy builder on the host), dm-thin, pytest 9 / pytest-asyncio / pytest-cov, httpx, uv, ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-09-07-generative-agent-workload-proof-design.md` §5 (product changes), §6 (tests), §7 (documents and issues), §8 (validation). The test plan text is in the spec's appendix and already applied to `docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md`. Decisions taken here:

1. **`BASE_IMAGE` moves to `mshkn.services.recipes`** so the base-image rule and `base_volume.py` share it without an import cycle; `base_volume.py` re-imports it.
2. **The exit code uses `returncode`.** asyncssh's `SSHClientProcess.returncode` is the exit status or the negative signal number, so "128 + signal" is `128 - returncode` when `returncode < 0`; no signal-name table.
3. **T10.5's task is "the largest file under /root/data"**, which the explore step creates with three distinct sizes, instead of "under /etc": two files of equal size in `/etc` would make the two attempts disagree by accident, and the test is about the loop, not about `/etc`.
4. **Labels in T10.5 carry a per-run suffix** so a crashed earlier run cannot leave a checkpoint that satisfies "exactly one".

## Global Constraints

- Python `>=3.12`; uv only; every command runs as `uv run <tool>` inside the worktree.
- The gate, identical to CI: `uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov`. Green at the end of every task; zero warnings; the coverage floor (`fail_under` in `pyproject.toml`, 98 %) holds.
- No xfail, no new skips, no `# type: ignore` in tests, no assertion-free tests. Flow tests never patch module attributes; they read fake-host state.
- Product changes in this PR are exactly: `dockerfile_base_image` and the rule in `RecipeService.create`; `ExecRequest.timeout_seconds` and its threading; `_exit_code` in `mshkn.host.ssh` and the command in the timeout log line; image retention in `RecipeService.build` and removal in `RecipeService.delete`; `FakeGuest.stream_timeouts`. Nothing else under `src/` changes behaviour.
- Every E2E test destroys its computers, deletes its checkpoints and (T10.8) its recipes in `finally`. Recipes shared by content hash resolve immediately after the first build on a host.
- Every recipe must reach `ready` within 600 s; `create_recipe`'s timeout becomes 600.
- Live E2E gate for this PR: **153 passed, 6 skipped, 4 failed**, the four being T8.6 (`test_checkpoint_data_not_publicly_accessible`), T9.1 (`test_checkpoint_storage_cost_per_gb`), T11.2 (`test_logs_are_json`) and T11.7 (`test_create_destroy_logged`). Any other failure is a regression: fix it or stop and discuss. The suite collects 161.
- PR 7a (one base) must be on `main` first: T10.2 runs apt on a bare computer, which only the new base has.
- Commit messages end with the trailer block (Co-Authored-By and Claude-Session lines). Never merge; open the PR and request authorization.

---

## File Structure

**Created**
- `tests/unit/test_dockerfile_base.py` — parser cases for `dockerfile_base_image`.
- `tests/flow/test_recipe_rule_and_exec_timeout.py` — the 422 for a wrong base and the `timeout_seconds` threading and bounds, through HTTP on the fake host.
- `docs/superpowers/plans/2026-09-07-pr7-baseline.txt`.

**Modified**
- `src/mshkn/services/recipes.py` — `BASE_IMAGE`, `recipe_image_tag`, `dockerfile_base_image`, the rule in `create`, image kept on success and removed on failure and on `delete`.
- `src/mshkn/services/base_volume.py` — imports `BASE_IMAGE` from recipes.
- `src/mshkn/api/schemas.py` — `ExecRequest.timeout_seconds`.
- `src/mshkn/api/computers.py` — passes `timeout=body.timeout_seconds` to `stream`.
- `src/mshkn/services/computers.py` — `stream(..., *, timeout)`.
- `src/mshkn/host/ssh.py` — `_exit_code`, `_pump(process, timeout, command)`.
- `src/mshkn/host/fake.py` — `FakeGuest.stream_timeouts`.
- `tests/unit/test_ssh_guest.py` — fakes gain `returncode`; two exit-code tests.
- `tests/unit/test_recipe_service.py`, `tests/unit/test_recipes_edges.py` — Dockerfiles start `FROM mshkn-base`; retention tests.
- `tests/e2e/conftest.py` — `exec_command(timeout_seconds=)`, `wait_for_recipe`, `upload_file`, `port_url`; `create_recipe` timeout 600.
- `tests/e2e/test_phase4_networking.py` — imports `port_url` from conftest.
- `tests/e2e/test_phase3_capabilities.py` — the bad Dockerfile fails during the build.
- `tests/e2e/test_phase7_api.py` — T7.9.
- `tests/e2e/test_phase10_integration.py` — rewritten.
- `README.md`, `CLAUDE.md`, `DEPLOY.md`, `docs/ARCHITECTURE.md`, `docs/infrastructure.md`, `docs/plans/README.md`.

---

### Task 1: Worktree and baseline

**Files:**
- Create: `docs/superpowers/plans/2026-09-07-pr7-baseline.txt`

- [ ] **Step 1:** Confirm PR 7a is merged: `git log --oneline origin/main | head -5` shows its merge. Use `superpowers:using-git-worktrees` to create `../mshkn-pr7` on branch `pr7-workload-proof` from `main`. `cd ../mshkn-pr7 && uv sync --frozen`.

- [ ] **Step 2:** Record the baseline:

```bash
{ echo "Baseline before PR 7 (main @ $(git rev-parse --short HEAD), $(date -I))"; uv run ruff check . | tail -1; uv run ruff format --check . | tail -1; uv run mypy | tail -1; uv run pytest -q -p no:cacheprovider 2>&1 | tail -1; uv run pytest --cov -q -p no:cacheprovider 2>&1 | grep TOTAL; } | tee docs/superpowers/plans/2026-09-07-pr7-baseline.txt
git add docs/superpowers/plans/2026-09-07-pr7-baseline.txt && git commit -m "chore: record pre-PR7 baseline"
```

Expected: clean; the unit and flow count PR 7a left (about 505 passed); coverage at or above 98 %. Report the actual numbers.

---

### Task 2: The base-image rule (#73)

**Files:**
- Modify: `src/mshkn/services/recipes.py` (module level; `RecipeService.create`)
- Modify: `src/mshkn/services/base_volume.py` (`BASE_IMAGE` import)
- Modify: `tests/unit/test_recipe_service.py`, `tests/unit/test_recipes_edges.py`, and any other unit or flow test that posts a Dockerfile not based on `mshkn-base`
- Test: `tests/unit/test_dockerfile_base.py`, `tests/flow/test_recipe_rule_and_exec_timeout.py`

**Interfaces:**
- Produces, in `mshkn.services.recipes`:

```python
BASE_IMAGE = "mshkn-base"

def dockerfile_base_image(dockerfile: str) -> str | None:
    """The image of the last FROM (the stage that is exported), or None without a FROM."""

def image_name(reference: str) -> str:
    """'mshkn-base' for 'mshkn-base', 'mshkn-base:latest' and 'mshkn-base@sha256:…'."""
```

`RecipeService.create` raises `InvalidInput("Dockerfile has no FROM instruction")` or `InvalidInput(f"recipes must be built FROM {BASE_IMAGE} (the final stage is FROM {base})")` before the content-hash lookup; the router maps `InvalidInput` to 422 already.

- [ ] **Step 1: Write the failing parser tests**

`tests/unit/test_dockerfile_base.py`:

```python
"""dockerfile_base_image: the last FROM decides what gets exported, so it decides the rule."""

from __future__ import annotations

import pytest

from mshkn.services.recipes import dockerfile_base_image, image_name


@pytest.mark.parametrize(
    ("dockerfile", "expected"),
    [
        ("FROM mshkn-base\nRUN true", "mshkn-base"),
        ("from mshkn-base:latest as base\nRUN true", "mshkn-base:latest"),
        ("# syntax=docker/dockerfile:1\n\nARG TAG=latest\nFROM mshkn-base:${TAG}\n", "mshkn-base:${TAG}"),
        ("FROM --platform=linux/amd64 mshkn-base AS build\nRUN true", "mshkn-base"),
        ("FROM golang:1.22 AS build\nRUN go build\nFROM mshkn-base\nCOPY --from=build /a /a", "mshkn-base"),
        ("FROM mshkn-base AS base\nFROM scratch\nCOPY --from=base / /", "scratch"),
        ("FROM python:3.12\nRUN true", "python:3.12"),
        ("  FROM   ubuntu@sha256:abc  ", "ubuntu@sha256:abc"),
        ("RUN true\n# FROM mshkn-base in a comment does not count", None),
        ("", None),
    ],
)
def test_last_from_wins(dockerfile: str, expected: str | None) -> None:
    assert dockerfile_base_image(dockerfile) == expected


@pytest.mark.parametrize(
    ("reference", "name"),
    [
        ("mshkn-base", "mshkn-base"),
        ("mshkn-base:latest", "mshkn-base"),
        ("mshkn-base@sha256:0123", "mshkn-base"),
        ("python:3.12", "python"),
        ("ghcr.io/org/img:1", "ghcr.io/org/img"),
    ],
)
def test_image_name_strips_tag_and_digest(reference: str, name: str) -> None:
    assert image_name(reference) == name
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_dockerfile_base.py -v`
Expected: FAIL with `ImportError: cannot import name 'dockerfile_base_image'`.

- [ ] **Step 3: Implement the parser and the rule**

In `src/mshkn/services/recipes.py`, add after `_DOCKER_BUILD_TIMEOUT_SECONDS`:

```python
BASE_IMAGE = "mshkn-base"

# `FROM [--flag=value ...] <image> [AS <name>]`, any case; the image is the first
# token that is not a flag.
_FROM_RE = re.compile(r"^FROM\s+(?:--\S+\s+)*(\S+)", re.IGNORECASE)


def dockerfile_base_image(dockerfile: str) -> str | None:
    """The image of the last FROM (the stage that is exported), or None without a FROM.

    Comments, blank lines, parser directives and ARG lines before the first
    FROM are skipped; a multi-stage build is judged by its final stage,
    because that is the filesystem `docker export` produces.
    """
    image: str | None = None
    for raw in dockerfile.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _FROM_RE.match(line)
        if match:
            image = match.group(1)
    return image


def image_name(reference: str) -> str:
    """'mshkn-base' for 'mshkn-base', 'mshkn-base:latest' and 'mshkn-base@sha256:…'."""
    name = reference.split("@", 1)[0]
    # The tag follows the last colon, unless that colon belongs to a registry port.
    head, sep, tail = name.rpartition(":")
    if sep and "/" not in tail:
        return head
    return name
```

`re` is already imported. Import `InvalidInput` alongside `Conflict, NotFound` from `mshkn.errors`. At the top of `RecipeService.create`, before `content_hash = …`:

```python
        base = dockerfile_base_image(dockerfile)
        if base is None:
            raise InvalidInput("Dockerfile has no FROM instruction")
        if image_name(base) != BASE_IMAGE:
            raise InvalidInput(
                f"recipes must be built FROM {BASE_IMAGE} (the final stage is FROM {base})"
            )
```

In `src/mshkn/services/base_volume.py`, delete `BASE_IMAGE = "mshkn-base"` and add `BASE_IMAGE` to the `from mshkn.services.recipes import …` line (keep it exported from the module: the CLI imports it from there).

- [ ] **Step 4: Fix the unit and flow tests that post other bases**

`grep -rn '"FROM ' tests/unit tests/flow` lists every Dockerfile literal. Change each one whose base is not `mshkn-base` to start `FROM mshkn-base\n`, keeping what the test is about: `"FROM nope"` → `"FROM mshkn-base\nRUN false"`, `"FROM x"` → `"FROM mshkn-base\nRUN true"`, `"FROM a"`/`"FROM b"` in `test_dockerfile_content_hash` stay (the hash function does not validate). Where a flow test creates a recipe with `{"dockerfile": "FROM …"}`, same rule. Then run `uv run pytest tests/unit tests/flow -q` → everything passes except the new files.

- [ ] **Step 5: Write the failing flow test**

`tests/flow/test_recipe_rule_and_exec_timeout.py` (the exec half is added in Task 3):

```python
"""Two agent-facing rules through HTTP: recipes must end FROM mshkn-base, and
the exec time limit belongs to the caller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.db import list_recipes_by_account

if TYPE_CHECKING:
    import pytest

    from .conftest import Flow


async def test_a_recipe_from_another_base_is_422_before_any_build(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The accepted recipe at the end would otherwise start a real docker build;
    # the flow tier replaces the build and the shell on the service instance,
    # as tests/flow/test_recipes.py does.
    async def build_image(cmd: str) -> str:
        return "ok"

    async def run(cmd: str, check: bool = True) -> str:
        return ""

    monkeypatch.setattr(flow.runtime.recipes, "_build_image", build_image)
    monkeypatch.setattr(flow.runtime.recipes, "_run", run)

    resp = await flow.client.post("/recipes", json={"dockerfile": "FROM python:3.12\nRUN true"})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "mshkn-base" in detail and "python:3.12" in detail
    assert await list_recipes_by_account(flow.runtime.db, "acct-1") == []
    assert flow.host.blocks.calls == []  # no snap, no mkfs: nothing was built

    resp = await flow.client.post("/recipes", json={"dockerfile": "RUN true"})
    assert resp.status_code == 422
    assert "no FROM" in resp.json()["detail"]

    # A multi-stage build whose final stage is mshkn-base is accepted.
    ok = await flow.client.post(
        "/recipes",
        json={"dockerfile": "FROM golang:1.22 AS build\nFROM mshkn-base\nCOPY --from=build /a /a"},
    )
    assert ok.status_code == 202, ok.text
    await flow.runtime.tasks.wait(f"recipe_build:{ok.json()['recipe_id']}")
    assert (await flow.client.get(f"/recipes/{ok.json()['recipe_id']}")).json()["status"] == "ready"
```

Check `list_recipes_by_account` is exported from `mshkn.db` (it is imported in `recipes.py` from there).

- [ ] **Step 6: Run everything**

Run: `uv run pytest tests/unit/test_dockerfile_base.py tests/flow/test_recipe_rule_and_exec_timeout.py tests/unit/test_recipe_service.py tests/unit/test_recipes_edges.py tests/unit/test_base_volume.py -v` → all PASS.

- [ ] **Step 7: Gate and commit**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov -q` → clean, floor held.

```bash
git add -A
git commit -m "feat(recipes): reject a Dockerfile whose final stage is not FROM mshkn-base (#73)"
```

---

### Task 3: `timeout_seconds` on exec

**Files:**
- Modify: `src/mshkn/api/schemas.py:39-41`, `src/mshkn/api/computers.py:70-91`, `src/mshkn/services/computers.py:395-398`, `src/mshkn/host/fake.py` (`FakeGuest.__init__`, `stream`)
- Test: `tests/flow/test_recipe_rule_and_exec_timeout.py` (extend)

**Interfaces:**
- `ExecRequest(command: str, timeout_seconds: int = Field(default=60, ge=1, le=600))`.
- `ComputerService.stream(self, computer: Computer, command: str, *, timeout: float = 60.0) -> AsyncIterator[OutputLine]`.
- `FakeGuest.stream_timeouts: list[float]`, appended on every `stream` call.

- [ ] **Step 1: Write the failing flow tests**

Append to `tests/flow/test_recipe_rule_and_exec_timeout.py`:

```python
async def test_exec_timeout_is_the_callers_and_bounded(flow: Flow) -> None:
    cid = (await flow.client.post("/computers", json={})).json()["computer_id"]

    async with flow.client.stream(
        "POST", f"/computers/{cid}/exec", json={"command": "true"}
    ) as resp:
        assert resp.status_code == 200
        await resp.aread()
    async with flow.client.stream(
        "POST", f"/computers/{cid}/exec", json={"command": "pip install pandas", "timeout_seconds": 300}
    ) as resp:
        assert resp.status_code == 200
        await resp.aread()
    assert flow.host.guest.stream_timeouts == [60.0, 300.0]

    for bad in (0, 601, -5):
        resp = await flow.client.post(
            f"/computers/{cid}/exec", json={"command": "true", "timeout_seconds": bad}
        )
        assert resp.status_code == 422, (bad, resp.text)
    assert flow.host.guest.stream_timeouts == [60.0, 300.0]  # rejected before reaching the guest
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/flow/test_recipe_rule_and_exec_timeout.py -v -k timeout`
Expected: FAIL with `AttributeError: 'FakeGuest' object has no attribute 'stream_timeouts'`.

- [ ] **Step 3: Implement**

`src/mshkn/api/schemas.py`:

```python
class ExecRequest(BaseModel):
    command: str
    timeout_seconds: int = Field(default=60, ge=1, le=600)
```

(`Field` is already imported for the ingress models.)

`src/mshkn/api/computers.py`, in `exec_command`'s `event_stream`:

```python
                async for stream, line in rt.computers.stream(
                    computer, body.command, timeout=float(body.timeout_seconds)
                ):
```

`src/mshkn/services/computers.py`:

```python
    async def stream(
        self, computer: Computer, command: str, *, timeout: float = 60.0
    ) -> AsyncIterator[OutputLine]:
        await self._touch(computer)
        async for item in self.host.guest.stream(computer.vm_ip, command, timeout=timeout):
            yield item
```

`src/mshkn/host/fake.py`: in `FakeGuest.__init__` add `self.stream_timeouts: list[float] = []`; in `stream`, remove the `# noqa: ARG002` on `timeout` and add `self.stream_timeouts.append(timeout)` right after `self._maybe_fail("stream")`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/flow -q` → all pass (the existing exec flow tests still see `("exit", "42")` and friends).

- [ ] **Step 5: Gate and commit**

Run the gate → clean.

```bash
git add src/mshkn/api/schemas.py src/mshkn/api/computers.py src/mshkn/services/computers.py src/mshkn/host/fake.py tests/flow/test_recipe_rule_and_exec_timeout.py
git commit -m "feat(exec): timeout_seconds on POST /computers/{id}/exec, 60 by default, 600 at most"
```

---

### Task 4: An honest exit after a kill

**Files:**
- Modify: `src/mshkn/host/ssh.py` (`stream`, `_pump`, new `_exit_code`)
- Test: `tests/unit/test_ssh_guest.py`

**Interfaces:**
- `_exit_code(process) -> str`: `exit_status` when known; else `128 - returncode` when `returncode < 0` (a signal); else `"255"`.
- `_pump(process, timeout, command)`: the timeout warning names the command.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_ssh_guest.py`, add `self.returncode: int | None = None` to `FakeProcess.__init__`, `LateStatusProcess.__init__`, `LostConnectionProcess.__init__` and `DropAfterExitProcess.__init__` (asyncssh exposes both attributes; the fakes now do too). Add two fakes after `DropAfterExitProcess`:

```python
class KilledProcess:
    """A command that ignores the deadline: no exit status, a signal once killed.

    asyncssh reports a signalled process with exit_status None and
    returncode = -signal; SIGKILL is 9.
    """

    def __init__(self) -> None:
        self.stdout = FakeReader([(0.0, "working\n")], hang_after=True)
        self.stderr = FakeReader([], hang_after=True)
        self.exit_status: int | None = None
        self.returncode: int | None = None
        self.killed = False

    async def wait(self) -> None:
        while not self.killed:
            await asyncio.sleep(0.01)
        self.returncode = -9

    def kill(self) -> None:
        self.killed = True


class StatuslessProcess:
    """The channel closed with neither an exit status nor a signal."""

    def __init__(self) -> None:
        self.stdout = FakeReader([(0.0, "a\n")])
        self.stderr = FakeReader([])
        self.exit_status: int | None = None
        self.returncode: int | None = None
        self.killed = False

    async def wait(self) -> None:
        return None

    def kill(self) -> None:
        self.killed = True
```

And two tests after `test_stream_kills_on_timeout_and_still_reports_exit`:

```python
async def test_a_killed_command_reports_128_plus_the_signal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(ssh_module, "STREAM_GRACE_SECONDS", 0.1)
    process = KilledProcess()
    guest = make_guest(process)
    with caplog.at_level("WARNING", logger="mshkn.host.ssh"):
        items = [item async for item in guest.stream("172.16.1.2", "sleep 30", timeout=0.1)]
    assert process.killed
    assert items == [("stdout", "working"), ("exit", "137")]
    assert any("sleep 30" in rec.getMessage() for rec in caplog.records)


async def test_a_process_with_neither_status_nor_signal_reports_255() -> None:
    guest = make_guest(StatuslessProcess())
    items = [item async for item in guest.stream("172.16.1.2", "cmd")]
    assert items == [("stdout", "a"), ("exit", "255")]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_ssh_guest.py -v -k "killed or statusless"`
Expected: both FAIL: the exit event is `"0"`.

- [ ] **Step 3: Implement**

In `src/mshkn/host/ssh.py`, add above `class SshGuest`:

```python
def _exit_code(process: asyncssh.SSHClientProcess[str]) -> str:
    """The exit event's payload: the status, else 128 + the signal, else 255.

    A process the timeout killed has no exit status, only an exit signal;
    reporting it as 0 would tell the caller a killed command succeeded.
    """
    if process.exit_status is not None:
        return str(process.exit_status)
    returncode = process.returncode  # asyncssh: negative signal number when signalled
    if returncode is not None and returncode < 0:
        return str(128 - returncode)
    return "255"
```

Change `_pump`'s signature to `async def _pump(process, timeout: float, command: str)` (keeping the existing type annotations), the call in `stream` to `self._pump(process, timeout, command)`, the timeout warning to `logger.warning("stream: %r did not exit within %.1fs, killing", command, timeout)`, and the final line to `yield ("exit", _exit_code(process))`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_ssh_guest.py -v` → all PASS, including `test_stream_kills_on_timeout_and_still_reports_exit` (its fake still carries `exit_status=0`, which is the known-status branch).

- [ ] **Step 5: Gate and commit**

Run the gate → clean.

```bash
git add src/mshkn/host/ssh.py tests/unit/test_ssh_guest.py
git commit -m "fix(ssh): a killed command reports 128 + signal, never exit 0"
```

---

### Task 5: Recipe image retention

**Files:**
- Modify: `src/mshkn/services/recipes.py` (`recipe_image_tag`, `build`, `delete`)
- Test: `tests/unit/test_recipe_service.py`

**Interfaces:**
- `recipe_image_tag(recipe_id: str) -> str` returns `f"mshkn-recipe-img-{recipe_id}"`.
- `build` keeps the image after a successful export; on any failure it runs `docker rmi -f <tag>` (unchecked). `delete` runs `docker rmi -f <tag>` (unchecked) after removing the volume.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_recipe_service.py`:

```python
async def test_a_successful_build_keeps_its_image_for_the_layer_cache(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    await insert_account(db, ACCOUNT)
    service, _, shell = _service(db, tmp_path)
    recipe, _ = await service.create(ACCOUNT, "FROM mshkn-base\nRUN true")
    await service.tasks.wait(service.build_task_name(recipe.id))
    assert not any("docker rmi" in c for c in shell.calls)


async def test_a_failed_build_removes_its_image(db: aiosqlite.Connection, tmp_path: Path) -> None:
    await insert_account(db, ACCOUNT)
    service, _, shell = _service(db, tmp_path, shell=FakeShell(fail_on="tar xf"))
    recipe, _ = await service.create(ACCOUNT, "FROM mshkn-base\nRUN true")
    await service.tasks.wait(service.build_task_name(recipe.id))
    assert f"docker rmi -f {recipe_image_tag(recipe.id)}" in shell.calls


async def test_delete_removes_the_image_with_the_volume(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    await insert_account(db, ACCOUNT)
    service, host, shell = _service(db, tmp_path)
    recipe, _ = await service.create(ACCOUNT, "FROM mshkn-base\nRUN true")
    await service.tasks.wait(service.build_task_name(recipe.id))
    await service.delete(ACCOUNT, recipe.id)
    assert ("remove", (100, f"mshkn-recipe-{recipe.id}")) in host.blocks.calls
    assert shell.calls[-1] == f"docker rmi -f {recipe_image_tag(recipe.id)}"
```

Import `recipe_image_tag` from `mshkn.services.recipes`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_recipe_service.py -v -k image`
Expected: ImportError on `recipe_image_tag`, then (after adding only the name) the first test fails because `docker rmi` runs in `finally`.

- [ ] **Step 3: Implement**

In `src/mshkn/services/recipes.py`:

```python
def recipe_image_tag(recipe_id: str) -> str:
    """The Docker image a recipe's build leaves behind; its layers are the rebuild cache."""
    return f"mshkn-recipe-img-{recipe_id}"
```

In `build`: `image_tag = recipe_image_tag(recipe_id)`; in the `except Exception` branch, after the `update_recipe_build_result(... FAILED ...)`, add:

```python
            with contextlib.suppress(Exception):
                await self._run(f"docker rmi -f {image_tag}", check=False)
```

and delete the `docker rmi` lines from `finally`. Update `build`'s docstring: `"""Docker build → export → inject into dm-thin → ready (the image stays as the layer cache); failed with a log otherwise."""`.

In `delete`, after the volume removal:

```python
        with contextlib.suppress(Exception):
            await self._run(f"docker rmi -f {recipe_image_tag(recipe_id)}", check=False)
```

- [ ] **Step 4: Run the tests and the gate**

Run: `uv run pytest tests/unit/test_recipe_service.py tests/unit/test_recipes_edges.py -v` → all PASS. Gate → clean.

```bash
git add src/mshkn/services/recipes.py tests/unit/test_recipe_service.py
git commit -m "feat(recipes): keep the built image so appended-layer rebuilds hit the cache; remove it with the recipe"
```

---

### Task 6: E2E helpers, T7.9, and the phase 3 build-failure test

**Files:**
- Modify: `tests/e2e/conftest.py` (`exec_command`, `create_recipe`; new `wait_for_recipe`, `upload_file`, `port_url`)
- Modify: `tests/e2e/test_phase4_networking.py:8,33-40` (import `port_url` from conftest, delete the local one and the `urlparse` import)
- Modify: `tests/e2e/test_phase3_capabilities.py:112-137`
- Modify: `tests/e2e/test_phase7_api.py` (append T7.9)

**Interfaces:**
- `exec_command(client, computer_id, command, timeout=30.0, *, timeout_seconds: int | None = None) -> ExecResult`: when `timeout_seconds` is given it goes in the body and the HTTP timeout becomes `max(timeout, timeout_seconds + 30)`.
- `wait_for_recipe(client, recipe_id, timeout=600.0) -> tuple[dict[str, Any], float]`: polls every 3 s to `ready` or `failed`; returns the body and the seconds elapsed inside the call; raises `TimeoutError` past the deadline.
- `create_recipe(client, dockerfile, timeout=600.0) -> str`: unchanged contract, built on `wait_for_recipe`.
- `upload_file(client, computer_id, path, data: bytes) -> None`.
- `port_url(base_url, port) -> str` (moved).

- [ ] **Step 1: conftest**

Replace `create_recipe` with:

```python
async def wait_for_recipe(
    client: httpx.AsyncClient, recipe_id: str, timeout: float = 600.0
) -> tuple[dict[str, Any], float]:
    """Poll GET /recipes/{id} to a terminal status; (body, seconds waited)."""
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        r = await client.get(f"/recipes/{recipe_id}")
        r.raise_for_status()
        info: dict[str, Any] = r.json()
        if info["status"] in ("ready", "failed"):
            return info, time.monotonic() - started
        await asyncio.sleep(3)
    raise TimeoutError(f"Recipe {recipe_id} did not complete in {timeout}s")


async def create_recipe(client: httpx.AsyncClient, dockerfile: str, timeout: float = 600.0) -> str:
    """Create a recipe, wait for it to be ready, return recipe_id."""
    resp = await client.post("/recipes", json={"dockerfile": dockerfile})
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    recipe_id: str = data["recipe_id"]
    if data["status"] == "ready":
        return recipe_id
    info, _ = await wait_for_recipe(client, recipe_id, timeout)
    if info["status"] != "ready":
        raise RuntimeError(f"Recipe build failed: {info.get('build_log', '')[:500]}")
    return recipe_id
```

Change `exec_command`'s signature and body:

```python
async def exec_command(
    client: httpx.AsyncClient,
    computer_id: str,
    command: str,
    timeout: float = 30.0,
    *,
    timeout_seconds: int | None = None,
) -> ExecResult:
    """Execute a command via SSE and return parsed output.

    timeout is the HTTP read timeout; timeout_seconds is the server-side limit
    (60 by default) and, when given, stretches the HTTP timeout to cover it.
    """
    body: dict[str, object] = {"command": command}
    if timeout_seconds is not None:
        body["timeout_seconds"] = timeout_seconds
        timeout = max(timeout, timeout_seconds + 30)
    ...
    async with client.stream(
        "POST",
        f"/computers/{computer_id}/exec",
        json=body,
        timeout=timeout,
    ) as resp:
```

Add:

```python
async def upload_file(
    client: httpx.AsyncClient, computer_id: str, path: str, data: bytes
) -> None:
    """PUT a file into the VM through POST /computers/{id}/upload?path=."""
    resp = await client.post(
        f"/computers/{computer_id}/upload",
        params={"path": path},
        content=data,
        headers={"Content-Type": "application/octet-stream"},
    )
    resp.raise_for_status()


def port_url(base_url: str, port: int) -> str:
    """https://comp-abc.mshkn.dev → https://{port}-comp-abc.mshkn.dev."""
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{port}-{parsed.hostname}"
```

with `from urllib.parse import urlparse` at the top. In `test_phase4_networking.py`, delete the local `port_url` and the `urlparse` import, and add `port_url` to the `from .conftest import (…)` list. Check `tests/e2e/test_phase7_api.py::test_computer_upload_and_verify` for the exact upload call shape and match it in `upload_file`.

- [ ] **Step 2: Phase 3's build failure fails during the build**

In `tests/e2e/test_phase3_capabilities.py::TestBuildFailure.test_bad_dockerfile_fails`, replace the posted Dockerfile with `"FROM mshkn-base\nRUN false"`, replace the inline polling loop with `data, _ = await wait_for_recipe(long_client, recipe_id, timeout=600)`, drop the inline `import asyncio`/`import time`, and keep the three assertions (`failed`, `build_log` present and non-empty). Add `wait_for_recipe` to the conftest import. Update the class docstring: `"""A Dockerfile whose build fails produces status=failed with a build_log."""`.

- [ ] **Step 3: T7.9**

Append to `tests/e2e/test_phase7_api.py`:

```python
# ---------------------------------------------------------------------------
# T7.9 — The Exec Time Limit Belongs to the Caller
# ---------------------------------------------------------------------------


class TestT79ExecTimeLimit:
    """timeout_seconds bounds a command; a killed command never reports success."""

    async def test_a_long_command_survives_a_longer_limit(
        self, long_client: httpx.AsyncClient
    ) -> None:
        async with managed_computer(long_client) as cid:
            result = await exec_command(
                long_client, cid, "sleep 65 && echo slept", timeout_seconds=90
            )
            assert result.events[-1] == ("exit", "0"), result.events
            assert "slept" in result.stdout

    async def test_a_command_past_its_limit_is_killed_and_says_so(
        self, long_client: httpx.AsyncClient
    ) -> None:
        async with managed_computer(long_client) as cid:
            started = time.monotonic()
            result = await exec_command(long_client, cid, "sleep 30", timeout_seconds=2)
            elapsed = time.monotonic() - started
            assert elapsed < 10, f"the stream should end soon after the 2 s limit, took {elapsed:.1f}s"
            assert result.events[-1] == ("exit", "137"), result.events
```

`time` is already imported in that file.

- [ ] **Step 4: Static checks and collection**

Run: `uv run ruff check tests/e2e && uv run ruff format --check tests/e2e && uv run mypy && uv run pytest tests/e2e --collect-only -q -m e2e | tail -2` → clean; collection shows 159 (157 plus the two T7.9 tests). Run `uv run pytest -q` → unit and flow unchanged.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/conftest.py tests/e2e/test_phase4_networking.py tests/e2e/test_phase3_capabilities.py tests/e2e/test_phase7_api.py
git commit -m "test(e2e): timeout_seconds on exec_command, wait_for_recipe, upload_file; T7.9; phase 3 build failure fails in the build"
```

---

### Task 7: T10.1 and T10.2

**Files:**
- Rewrite: `tests/e2e/test_phase10_integration.py` (this task writes the module header, T10.1, T10.2, and keeps T10.3 and T10.4 verbatim; Tasks 8 and 9 append)

- [ ] **Step 1: Module header and T10.1**

Replace the file's docstring and imports with:

```python
"""Phase 10: The Generative Loop.

mshkn exists for a generative agent that writes its own recipes and the
programs those computers run. Phases 0 to 9 prove the primitives; this phase
proves the loop: write a recipe, build it, read the failure, fix it, run real
tools, checkpoint, fork, diverge, pick. Every test here is deterministic and
runs against the live host; none needs an LLM.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any

import httpx

from .conftest import (
    checkpoint_computer,
    create_computer,
    create_recipe,
    delete_checkpoint,
    destroy_computer,
    exec_command,
    fork_checkpoint,
    port_url,
    upload_file,
    wait_for_recipe,
)

if TYPE_CHECKING:
    from .conftest import ExecResult

BUILD_CAP_SECONDS = 600  # the documented docker build cap; every recipe must be ready within it


def _apt(*packages: str) -> str:
    return (
        "RUN apt-get update && apt-get install -y --no-install-recommends "
        + " ".join(packages)
        + " && rm -rf /var/lib/apt/lists/*"
    )


def _ok(result: ExecResult) -> str:
    """stdout of a command that must have exited 0."""
    assert result.events and result.events[-1] == ("exit", "0"), (
        f"command failed: events={result.events[-6:]} stderr={result.stderr[-500:]}"
    )
    return result.stdout.strip()


async def _file_size(client: httpx.AsyncClient, cid: str, path: str) -> int:
    """-1 when the file does not exist."""
    out = _ok(await exec_command(client, cid, f"stat -c %s {path} 2>/dev/null || echo -1"))
    return int(out.splitlines()[-1])
```

Then T10.1:

```python
# ---------------------------------------------------------------------------
# T10.1 — The ffmpeg Loop
# ---------------------------------------------------------------------------

FFMPEG_DOCKERFILE = "FROM mshkn-base\n" + _apt("ffmpeg") + "\n"


class TestT101FfmpegLoop:
    """Recipe → ready → create → run a real tool → checkpoint → fork → diverge."""

    async def test_recipe_create_run_checkpoint_fork_diverge(
        self, long_client: httpx.AsyncClient
    ) -> None:
        started = time.monotonic()
        recipe_id = await create_recipe(long_client, FFMPEG_DOCKERFILE, timeout=BUILD_CAP_SECONDS)
        print(f"T10.1 ffmpeg recipe ready in {time.monotonic() - started:.0f}s")

        cid = await create_computer(long_client, recipe_id=recipe_id)
        checkpoint_id: str | None = None
        forks: list[str] = []
        try:
            _ok(
                await exec_command(
                    long_client,
                    cid,
                    "ffmpeg -v error -f lavfi -i sine=frequency=440:duration=2 -y /root/tone.wav",
                    timeout_seconds=120,
                )
            )
            assert await _file_size(long_client, cid, "/root/tone.wav") > 0
            checkpoint_id = await checkpoint_computer(long_client, cid, label="ffmpeg-base")

            fork_a = await fork_checkpoint(long_client, checkpoint_id)
            forks.append(fork_a)
            fork_b = await fork_checkpoint(long_client, checkpoint_id)
            forks.append(fork_b)

            _ok(
                await exec_command(
                    long_client,
                    fork_a,
                    "ffmpeg -v error -i /root/tone.wav -y /root/tone.mp3",
                    timeout_seconds=120,
                )
            )
            _ok(
                await exec_command(
                    long_client,
                    fork_b,
                    "ffmpeg -v error -i /root/tone.wav -t 1 -y /root/short.wav",
                    timeout_seconds=120,
                )
            )

            # Each fork has its own output and not the other's.
            assert await _file_size(long_client, fork_a, "/root/tone.mp3") > 0
            assert await _file_size(long_client, fork_a, "/root/short.wav") == -1
            assert await _file_size(long_client, fork_b, "/root/short.wav") > 0
            assert await _file_size(long_client, fork_b, "/root/tone.mp3") == -1

            probe = "ffprobe -v error -show_entries format=duration -of csv=p=0 {}"
            mp3 = float(_ok(await exec_command(long_client, fork_a, probe.format("/root/tone.mp3"))))
            short = float(
                _ok(await exec_command(long_client, fork_b, probe.format("/root/short.wav")))
            )
            assert 1.8 <= mp3 <= 2.3, mp3
            assert 0.9 <= short <= 1.1, short
        finally:
            for fid in forks:
                await destroy_computer(long_client, fid)
            await destroy_computer(long_client, cid)
            if checkpoint_id:
                await delete_checkpoint(long_client, checkpoint_id)
```

- [ ] **Step 2: T10.2**

```python
# ---------------------------------------------------------------------------
# T10.2 — apt and pip Inside a Bare Computer
# ---------------------------------------------------------------------------

_ROWS = 200
_GROUPS = 4


def _dataset() -> list[tuple[int, int]]:
    """(group, value) rows the test and the VM agree on without a random source."""
    return [(i % _GROUPS, (i * 7) % 101) for i in range(_ROWS)]


_WRITE_CSV = """\
import csv
with open("/root/data.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["group", "value"])
    for i in range(%d):
        w.writerow([i %% %d, (i * 7) %% 101])
print("rows", %d)
""" % (_ROWS, _GROUPS, _ROWS)

_MEAN = """\
import pandas as pd
df = pd.read_csv("/root/data.csv")
open("/root/mean.txt", "w").write(f"{df.value.mean():.4f}\\n")
print(f"mean={df.value.mean():.4f}")
"""

_GROUP_SUMS = """\
import pandas as pd
df = pd.read_csv("/root/data.csv")
sums = df.groupby("group").value.sum()
with open("/root/sums.txt", "w") as f:
    for g, s in sums.items():
        f.write(f"{g}={s}\\n")
        print(f"{g}={s}")
"""


class TestT102AptAndPipOnBare:
    """The other egress path: apt and pip run inside the VM, not in docker build."""

    async def test_apt_pip_checkpoint_fork_compare(self, long_client: httpx.AsyncClient) -> None:
        rows = _dataset()
        expected_mean = sum(v for _, v in rows) / len(rows)
        expected_sums = {g: sum(v for gg, v in rows if gg == g) for g in range(_GROUPS)}

        cid = await create_computer(long_client)
        checkpoint_id: str | None = None
        forks: list[str] = []
        try:
            started = time.monotonic()
            _ok(
                await exec_command(
                    long_client,
                    cid,
                    "apt-get update && apt-get install -y --no-install-recommends "
                    "python3 python3-pip python3-venv",
                    timeout_seconds=300,
                )
            )
            print(f"T10.2 apt in {time.monotonic() - started:.0f}s")
            started = time.monotonic()
            _ok(
                await exec_command(
                    long_client,
                    cid,
                    "python3 -m venv /root/venv && /root/venv/bin/pip install -q pandas",
                    timeout_seconds=300,
                )
            )
            print(f"T10.2 pip in {time.monotonic() - started:.0f}s")

            await upload_file(long_client, cid, "/root/write_csv.py", _WRITE_CSV.encode())
            assert _ok(await exec_command(long_client, cid, "python3 /root/write_csv.py")) == (
                f"rows {_ROWS}"
            )
            checkpoint_id = await checkpoint_computer(long_client, cid, label="data-ready")

            fork_a = await fork_checkpoint(long_client, checkpoint_id)
            forks.append(fork_a)
            fork_b = await fork_checkpoint(long_client, checkpoint_id)
            forks.append(fork_b)
            await upload_file(long_client, fork_a, "/root/mean.py", _MEAN.encode())
            await upload_file(long_client, fork_b, "/root/sums.py", _GROUP_SUMS.encode())

            mean_out = _ok(
                await exec_command(
                    long_client, fork_a, "/root/venv/bin/python /root/mean.py", timeout_seconds=120
                )
            )
            assert mean_out == f"mean={expected_mean:.4f}", mean_out

            sums_out = _ok(
                await exec_command(
                    long_client, fork_b, "/root/venv/bin/python /root/sums.py", timeout_seconds=120
                )
            )
            got = {int(k): int(v) for k, v in (line.split("=") for line in sums_out.splitlines())}
            assert got == expected_sums, sums_out

            # Neither fork has the other's file.
            assert await _file_size(long_client, fork_a, "/root/sums.txt") == -1
            assert await _file_size(long_client, fork_b, "/root/mean.txt") == -1
        finally:
            for fid in forks:
                await destroy_computer(long_client, fid)
            await destroy_computer(long_client, cid)
            if checkpoint_id:
                await delete_checkpoint(long_client, checkpoint_id)
```

Keep the existing `TestT103ParallelExploration` and `TestT104FailureRecovery` classes after T10.2 exactly as they are (their section comments included). Delete the old `TestT105DumbAgentTest` (Task 8 replaces it).

- [ ] **Step 3: Static checks**

Run: `uv run ruff check tests/e2e && uv run ruff format --check tests/e2e && uv run mypy && uv run pytest tests/e2e --collect-only -q -m e2e | tail -1` → clean; the file collects 4 tests at this point.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_phase10_integration.py
git commit -m "test(e2e): T10.1 the ffmpeg loop; T10.2 apt and pip inside a bare computer"
```

---

### Task 8: T10.5 and T10.6

**Files:**
- Modify: `tests/e2e/test_phase10_integration.py` (append after T10.4)

- [ ] **Step 1: T10.5**

```python
# ---------------------------------------------------------------------------
# T10.5 — The Scripted Agent Loop
# ---------------------------------------------------------------------------


class ScriptedAgent:
    """A deterministic agent: the tool table is the API, the policy is the test.

    Everything it creates is recorded so `cleanup` can tear it down whatever
    the outcome; the policy also cleans up on its own, which is part of what
    the test asserts.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.computers: list[str] = []
        self.checkpoints: list[str] = []

    async def create(self) -> str:
        cid = await create_computer(self.client)
        self.computers.append(cid)
        return cid

    async def run(self, cid: str, command: str) -> str:
        return _ok(await exec_command(self.client, cid, command, timeout_seconds=60))

    async def checkpoint(self, cid: str, label: str) -> str:
        ckpt = await checkpoint_computer(self.client, cid, label=label)
        self.checkpoints.append(ckpt)
        return ckpt

    async def fork(self, ckpt: str) -> str:
        cid = await fork_checkpoint(self.client, ckpt)
        self.computers.append(cid)
        return cid

    async def list_label(self, label: str) -> list[dict[str, Any]]:
        resp = await self.client.get("/checkpoints", params={"label": label})
        resp.raise_for_status()
        result: list[dict[str, Any]] = resp.json()
        return result

    async def destroy(self, cid: str) -> None:
        resp = await self.client.delete(f"/computers/{cid}")
        resp.raise_for_status()

    async def forget(self, ckpt: str) -> None:
        resp = await self.client.delete(f"/checkpoints/{ckpt}")
        resp.raise_for_status()

    async def cleanup(self) -> None:
        for cid in self.computers:
            await destroy_computer(self.client, cid)
        for ckpt in self.checkpoints:
            await delete_checkpoint(self.client, ckpt)


class TestT105ScriptedAgentLoop:
    """Explore, checkpoint, fork two attempts, compare, pick, discard."""

    async def test_explore_fork_compare_pick(self, long_client: httpx.AsyncClient) -> None:
        run_id = uuid.uuid4().hex[:8]
        agent = ScriptedAgent(long_client)
        try:
            # Explore: the data the task is about, with three distinct sizes.
            root = await agent.create()
            await agent.run(
                root,
                "mkdir -p /root/data && head -c 1000 /dev/zero > /root/data/small.bin "
                "&& head -c 20000 /dev/zero > /root/data/medium.bin "
                "&& head -c 300000 /dev/zero > /root/data/large.bin",
            )
            listing = await agent.run(root, "ls /root/data")
            assert set(listing.split()) == {"small.bin", "medium.bin", "large.bin"}, listing
            explored = await agent.checkpoint(root, f"explored-{run_id}")

            # Two attempts at "the largest file under /root/data", by different means.
            attempt_a = await agent.fork(explored)
            attempt_b = await agent.fork(explored)
            answer_a = await agent.run(
                attempt_a,
                "find /root/data -type f -printf '%s %p\\n' | sort -rn | head -1 "
                "| awk '{print $2}' | tee /root/answer.txt",
            )
            answer_b = await agent.run(
                attempt_b,
                "find /root/data -type f -exec du -b {} + | sort -rn | head -1 "
                "| awk '{print $2}' | tee /root/answer.txt",
            )
            assert answer_a == answer_b == "/root/data/large.bin", (answer_a, answer_b)

            # Pick: the attempts agree; keep the first, discard the second.
            winner, loser = attempt_a, attempt_b
            answer_ckpt = await agent.checkpoint(winner, f"answer-{run_id}")
            loser_ckpt = await agent.checkpoint(loser, f"discarded-{run_id}")
            await agent.destroy(loser)
            await agent.forget(loser_ckpt)

            # The kept work is reachable by label and forks to the answer.
            kept = await agent.list_label(f"answer-{run_id}")
            assert [c["id"] for c in kept] == [answer_ckpt], kept
            verify = await agent.fork(answer_ckpt)
            assert await agent.run(verify, "cat /root/answer.txt") == "/root/data/large.bin"

            # The discarded attempt is gone: its computer and its checkpoint.
            status = await long_client.get(f"/computers/{loser}/status")
            assert status.status_code == 404 or status.json().get("status") == "destroyed", (
                status.text
            )
            assert await agent.list_label(f"discarded-{run_id}") == []
        finally:
            await agent.cleanup()
```

- [ ] **Step 2: T10.6**

```python
# ---------------------------------------------------------------------------
# T10.6 — A Listener Survives Checkpoint and Fork
# ---------------------------------------------------------------------------

PYTHON_DOCKERFILE = "FROM mshkn-base\nRUN apt-get update && apt-get install -y python3"
# Same text as test_phase4_networking's recipe, so the host has it built already.

_COUNTER_SERVER = """\
import http.server

count = 0


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global count
        count += 1
        body = str(count).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


# 0.0.0.0 on purpose: a restore gives the VM a new address, and a socket bound
# to the old one would not answer on the new slot.
http.server.HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
"""


async def _count(url: str) -> int:
    """GET url until it answers 200; the body is the server's request count."""
    async with httpx.AsyncClient(timeout=20.0) as public:
        for _ in range(30):
            try:
                resp = await public.get(url)
                if resp.status_code == 200:
                    return int(resp.text.strip())
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
    raise AssertionError(f"{url} never answered 200")


class TestT106ListenerSurvives:
    """An in-memory counter behind a bound socket survives checkpoint and fork."""

    async def test_counter_continues_on_the_fork_and_the_original(
        self, long_client: httpx.AsyncClient
    ) -> None:
        recipe_id = await create_recipe(long_client, PYTHON_DOCKERFILE, timeout=BUILD_CAP_SECONDS)
        cid = await create_computer(long_client, recipe_id=recipe_id)
        checkpoint_id: str | None = None
        fork_id: str | None = None
        try:
            await upload_file(long_client, cid, "/root/counter.py", _COUNTER_SERVER.encode())
            bg = await long_client.post(
                f"/computers/{cid}/exec/bg", json={"command": "python3 /root/counter.py"}
            )
            bg.raise_for_status()
            status = await long_client.get(f"/computers/{cid}/status")
            status.raise_for_status()
            original_url = port_url(status.json()["url"], 8080)

            assert await _count(original_url) == 1
            assert await _count(original_url) == 2
            assert await _count(original_url) == 3

            checkpoint_id = await checkpoint_computer(long_client, cid, label="listening")
            fork_id = await fork_checkpoint(long_client, checkpoint_id)
            fork_status = await long_client.get(f"/computers/{fork_id}/status")
            fork_status.raise_for_status()
            fork_url = port_url(fork_status.json()["url"], 8080)
            assert fork_url != original_url

            # Memory state and the bound socket came with the snapshot.
            assert await _count(fork_url) == 4
            # The original kept running, independently.
            assert await _count(original_url) == 4
            assert await _count(fork_url) == 5
        finally:
            if fork_id:
                await destroy_computer(long_client, fork_id)
            await destroy_computer(long_client, cid)
            if checkpoint_id:
                await delete_checkpoint(long_client, checkpoint_id)
```

- [ ] **Step 3: Static checks and commit**

Run: `uv run ruff check tests/e2e && uv run ruff format --check tests/e2e && uv run mypy` → clean.

```bash
git add tests/e2e/test_phase10_integration.py
git commit -m "test(e2e): T10.5 the scripted agent loop; T10.6 a listener survives checkpoint and fork"
```

---

### Task 9: T10.7 and T10.8

**Files:**
- Modify: `tests/e2e/test_phase10_integration.py` (append)

- [ ] **Step 1: T10.7**

```python
# ---------------------------------------------------------------------------
# T10.7 — Broken Recipe, Build Log, Fix
# ---------------------------------------------------------------------------

_MISSING_PACKAGE = "no-such-package-mshkn-e2e"


class TestT107BrokenRecipe:
    """The agent's first recipes will be wrong; every failure must say why, early."""

    async def test_wrong_base_is_rejected_before_any_build(
        self, long_client: httpx.AsyncClient
    ) -> None:
        resp = await long_client.post("/recipes", json={"dockerfile": "FROM python:3.12\nRUN true"})
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert "mshkn-base" in detail and "python:3.12" in detail, detail

    async def test_failed_build_names_the_cause_and_the_fix_builds(
        self, long_client: httpx.AsyncClient
    ) -> None:
        broken = "FROM mshkn-base\n" + _apt(_MISSING_PACKAGE) + "\n"
        resp = await long_client.post("/recipes", json={"dockerfile": broken})
        resp.raise_for_status()
        failed_id = resp.json()["recipe_id"]
        info, elapsed = await wait_for_recipe(long_client, failed_id, timeout=BUILD_CAP_SECONDS)
        assert info["status"] == "failed", info
        log = info["build_log"] or ""
        assert _MISSING_PACKAGE in log and "Unable to locate package" in log, log[-800:]
        print(f"T10.7 failed build reported in {elapsed:.0f}s")

        # The corrected text builds and boots with the tool.
        fixed_id = await create_recipe(
            long_client, "FROM mshkn-base\n" + _apt("jq") + "\n", timeout=BUILD_CAP_SECONDS
        )
        cid = await create_computer(long_client, recipe_id=fixed_id)
        try:
            assert "jq" in _ok(await exec_command(long_client, cid, "jq --version")).lower()
        finally:
            await destroy_computer(long_client, cid)

        # Resubmitting the broken text after a failure retries as a new recipe.
        again = await long_client.post("/recipes", json={"dockerfile": broken})
        again.raise_for_status()
        retry_id = again.json()["recipe_id"]
        assert retry_id != failed_id
        info, _ = await wait_for_recipe(long_client, retry_id, timeout=BUILD_CAP_SECONDS)
        assert info["status"] == "failed"
```

- [ ] **Step 2: T10.8**

```python
# ---------------------------------------------------------------------------
# T10.8 — Incremental Toolchain Growth
# ---------------------------------------------------------------------------


def _toolchain_dockerfile(nonce: str) -> str:
    """A node toolchain in several layers; the nonce makes every run a cold build."""
    return "\n".join(
        [
            "FROM mshkn-base",
            f"RUN echo run-{nonce}",
            _apt("nodejs", "npm", "ca-certificates"),
            "RUN npm install -g typescript",
            "RUN npm install -g esbuild",
            "RUN npm install -g prettier",
            "RUN mkdir -p /app && cd /app && npm init -y",
            "",
        ]
    )


class TestT108IncrementalGrowth:
    """The agent grows an image by appending to its Dockerfile; only the new layer is paid for."""

    async def test_appended_layer_rebuilds_in_under_half_the_cold_time(
        self, long_client: httpx.AsyncClient
    ) -> None:
        nonce = uuid.uuid4().hex[:8]
        cold_text = _toolchain_dockerfile(nonce)
        grown_text = cold_text + "RUN npm install -g nodemon\n"
        recipe_ids: list[str] = []
        cid: str | None = None
        try:
            started = time.monotonic()
            resp = await long_client.post("/recipes", json={"dockerfile": cold_text})
            resp.raise_for_status()
            cold_id = resp.json()["recipe_id"]
            recipe_ids.append(cold_id)
            info, _ = await wait_for_recipe(long_client, cold_id, timeout=BUILD_CAP_SECONDS)
            t_cold = time.monotonic() - started
            assert info["status"] == "ready", (info.get("build_log") or "")[-800:]
            assert t_cold <= BUILD_CAP_SECONDS

            started = time.monotonic()
            resp = await long_client.post("/recipes", json={"dockerfile": grown_text})
            resp.raise_for_status()
            grown_id = resp.json()["recipe_id"]
            assert grown_id != cold_id
            recipe_ids.append(grown_id)
            info, _ = await wait_for_recipe(long_client, grown_id, timeout=BUILD_CAP_SECONDS)
            t_incr = time.monotonic() - started
            assert info["status"] == "ready", (info.get("build_log") or "")[-800:]
            print(f"T10.8 cold build {t_cold:.0f}s, appended-layer rebuild {t_incr:.0f}s")
            assert t_incr < 0.5 * t_cold, (
                f"a rebuild that only appends a layer took {t_incr:.0f}s against a cold "
                f"{t_cold:.0f}s: the layer cache is not being used"
            )

            cid = await create_computer(long_client, recipe_id=grown_id)
            versions = _ok(
                await exec_command(
                    long_client,
                    cid,
                    "node --version && tsc --version && esbuild --version "
                    "&& prettier --version && nodemon --version",
                    timeout_seconds=60,
                )
            )
            assert len(versions.splitlines()) == 5, versions
            print("T10.8 root filesystem: " + _ok(await exec_command(long_client, cid, "df -m /")))
        finally:
            if cid:
                await destroy_computer(long_client, cid)
            # Newest first: the grown recipe shares layers with the cold one.
            for recipe_id in reversed(recipe_ids):
                resp = await long_client.delete(f"/recipes/{recipe_id}")
                assert resp.status_code == 200, (recipe_id, resp.text)
            # The template directory of the grown recipe stays behind until #75.
```

- [ ] **Step 3: Static checks and collection**

Run: `uv run ruff check tests/e2e && uv run ruff format --check tests/e2e && uv run mypy && uv run pytest tests/e2e --collect-only -q -m e2e | tail -1` → clean; **161 tests** collected (phase 10 collects 9: T10.1, T10.2, T10.3, T10.4, T10.5, T10.6, T10.7 ×2, T10.8). Run `uv run pytest -q` → unit and flow unchanged.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_phase10_integration.py
git commit -m "test(e2e): T10.7 broken recipe, build log, fix; T10.8 incremental toolchain growth"
```

---

### Task 10: Documents

**Files:**
- Modify: `README.md:10,15,28,69`, `CLAUDE.md:3,24`, `DEPLOY.md` (the Verify section's expectation line), `docs/ARCHITECTURE.md:134,158,174`, `docs/infrastructure.md:3`, `docs/plans/README.md`

- [ ] **Step 1: README.md**

- Line 10 (Exec): append after `over SSH`: `, bounded by a caller-chosen \`timeout_seconds\` (60 by default, 600 at most; a command past it is killed and reports exit 137)`.
- Line 15 (Recipes): `takes a Dockerfile that starts \`FROM mshkn-base\`` → `takes a Dockerfile whose final stage is \`FROM mshkn-base\` (anything else is a 422 before any build)`; append `; the built image is kept so a recipe that appends a layer rebuilds only that layer, and goes when the recipe is deleted`.
- Line 28: `Four of the 161 end-to-end tests describe checks that are not implemented and fail on purpose until they are (#65): the structured-log and audit-log checks, the checkpoint storage-cost measurement, and the R2 bucket-policy check.`
- Line 69: `It currently reports 153 passed, 6 skipped and 4 failed; the four are the unimplemented checks in #65, and anything else failing is a regression.`

- [ ] **Step 2: CLAUDE.md**

- Line 3: `(161 E2E tests)`.
- Line 24: `a full run takes about 26 minutes. The expected result is **153 passed, 6 skipped, 4 failed**, and the four must be exactly the \`Not implemented\` tests in #65.`

- [ ] **Step 3: DEPLOY.md**

The Verify section's line: `Expect **153 passed, 6 skipped, 4 failed**; the four failures are the \`Not implemented\` tests tracked in #65, and any other failure means the host or the deployment is wrong. The suite runs for about 26 minutes.`

- [ ] **Step 4: docs/ARCHITECTURE.md**

- §6 Exec paragraph (line 134), append: `\`timeout_seconds\` on the request (60 by default, 1 to 600) bounds the command: a process still running at the deadline is killed and its exit event is 128 plus the signal (137), and a process that ends with neither a status nor a signal reports 255, so a killed command never reads as a success.`
- §8 first paragraph (line 158): replace `by convention starting \`FROM mshkn-base\` … ; the service does not check that line, and nothing verifies the result boots until the first computer is created from it.` with `whose final stage must be \`FROM mshkn-base\` (…same parenthetical as PR 7a left…); \`RecipeService.create\` reads the last \`FROM\` (\`mshkn.services.recipes.dockerfile_base_image\`) and rejects any other base with \`InvalidInput\` before a row or a build exists, and nothing verifies the result boots until the first computer is created from it.` At the end of the paragraph add: `The built image (\`mshkn-recipe-img-<recipe id>\`) is kept: the host's Docker uses the legacy builder, whose cache is the image layer chain, so a recipe that appends a layer to an existing recipe's text rebuilds only that layer. \`RecipeService.delete\` removes the image with the volume, and a failed build removes its own.`
- §11 (line 174): `a stream whose process ignores exit is killed after 60 seconds` → `a stream whose process ignores exit is killed at its \`timeout_seconds\``.

- [ ] **Step 5: docs/infrastructure.md**

Line 3: `(\`tests/e2e/\`, 161 tests)`.

- [ ] **Step 6: docs/plans/README.md**

- The test plan row: `The definition of done: 161 end-to-end tests, T0 to T13.` and the status: `reference; the suite is \`tests/e2e/\`. Four tests (T8.6, T9.1, T11.2 and the audit-log check) fail as \`Not implemented\` until built (#65). Phase 10 and T7.9 were revised by #76 (\`docs/superpowers/specs/2026-09-07-generative-agent-workload-proof-design.md\`) and landed with PR 7.` Evidence: `\`tests/e2e/\` collects 161`.
- The PR 7 row: `| \`docs/superpowers/plans/2026-09-07-pr7-workload-proof.md\` | this PR | in progress (this PR) |`.
- In the roadmap breakdown, the last row: `| Economics validation (T9.x), S3 isolation (T8.6) | not implemented; part of #65 |` (the dumb agent entry goes: T10.5 is now the scripted loop).
- Open follow-ups: `#65 (four unimplemented E2E checks)`.

- [ ] **Step 7: Docs tests and the gate**

Run: `uv run pytest tests/unit/test_docs.py -v` → pass (`mshkn.services.recipes.dockerfile_base_image` resolves). Full gate → clean, zero warnings, floor held.

```bash
git add -A
git commit -m "docs: the exec limit, the base-image rule, image retention; the gate is 153/6/4 over 161 tests"
```

---

### Task 11: PR, CI, live E2E, issues

- [ ] **Step 1: Push and open the PR**

```bash
git push -u origin pr7-workload-proof
gh pr create --title "PR 7: the generative-agent workload proof (T10 rewritten, T7.9, #73, exec timeout, honest exit, image retention)" --body-file - <<'EOF'
Closes #73. Closes #76. Part of #65 (T10.1, T10.2 and T10.5 are now implemented; four remain).

**What this does**

Proves the loop a generative agent lives in, on the live host: eight Phase 10 tests (ffmpeg loop, apt and pip on a bare computer, parallel exploration, failure recovery, a scripted agent loop, a listener surviving checkpoint and fork, broken recipe → build log → fix, incremental toolchain growth) and T7.9 for the exec limit. Ships the four product changes the proof cannot be written without: `POST /recipes` rejects a Dockerfile whose final stage is not `FROM mshkn-base` (#73), `timeout_seconds` on exec, a killed command reports 128 + signal instead of 0, and recipe images are kept so appended-layer rebuilds hit the cache.

**Design alignment**

- Spec §5.1: the rule is on the last `FROM`; multi-stage builds ending in `mshkn-base` are accepted (flow test).
- Spec §5.2: `timeout_seconds` default 60, bounds 1 to 600, threaded to `Guest.stream`; the fake records it.
- Spec §5.3: `_exit_code` in `mshkn.host.ssh`: status, else 128 − returncode, else 255 (unit tests over fakes).
- Spec §5.4: image kept on success, removed on failure and on delete (unit tests); no buildx on the host.
- Architecture §1: `InvalidInput` → 422 through the existing mapping; no new routes.
- Test plan: Phase 10 and T7.9 as revised by #76.

**Validation performed**

- Gate: <paste>
- CI: <link>
- Live E2E: <summary line>; failing set: T8.6, T9.1, T11.2, T11.7 audit (all `Not implemented`, #65)
- T10.8 timings from the log: cold <n>s, appended layer <n>s
- Journal tracebacks after the run: <count>

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_013RCk2TTjv7RTFp2kHDcDHq
EOF
gh pr checks --watch
```

- [ ] **Step 2: Run the live suite, detached**

```bash
export MSHKN_SERVER=mshkn MSHKN_API_URL=http://65.21.22.161:8000
setsid nohup scripts/e2e.sh > /tmp/e2e-pr7.log 2>&1 < /dev/null &
```

Poll `tail -3 /tmp/e2e-pr7.log` until the summary line (about 26 minutes). Expected: `153 passed, 6 skipped, 4 failed`; `grep -E "^FAILED" /tmp/e2e-pr7.log` lists exactly `test_checkpoint_data_not_publicly_accessible`, `test_checkpoint_storage_cost_per_gb`, `test_logs_are_json`, `test_create_destroy_logged`. Then `grep "T10\.[0-9]" /tmp/e2e-pr7.log` for the printed timings, and:

```bash
ssh mshkn "journalctl -u mshkn --since '40 min ago' --no-pager | grep -ci traceback"
```

Expected `0`.

If T10.8's incremental bound fails, read the recipe's `build_log` on the host (`sqlite3 /opt/mshkn/mshkn.db "select build_log from recipes where id='<grown id>'"`): every layer up to the appended one must say `---> Using cache`. If it does not, the retained image is not being found by the legacy builder; stop and discuss rather than loosen the bound.

- [ ] **Step 3: Fill in the PR body**

`gh pr edit <N> --body-file -` with the gate output, the CI link, the summary line, the four names, the T10.8 timings and the traceback count. Triage bot comments per CLAUDE.md. Do not merge; ask for authorization.

- [ ] **Step 4: Record the decisions on the issues**

```bash
gh issue comment 73 --body "Decided in #76 (spec §5.1) and shipped in PR 7: enforce. POST /recipes rejects a Dockerfile whose *final* stage is not FROM mshkn-base with 422 naming the rule and the image, before any row or build exists. Last stage rather than first so a multi-stage build that compiles in a public image and copies into mshkn-base still works. E2E: T10.7; flow: tests/flow/test_recipe_rule_and_exec_timeout.py."
gh issue comment 58 --body "Decided in #76 (spec §2, item 6): after PR 7 and after #70, as its own PR. None of the nine PR 7 tests needs ephemeral exec output; every exec in them is on a live computer whose output comes back in the response."
gh issue comment 65 --body "PR 7 implements T10.1, T10.2 and T10.5 as revised by #76. Four remain: T8.6 (R2 bucket policy), T9.1 (storage cost), T11.2 (structured logs), T11.7 (audit log). The live gate is now 153 passed, 6 skipped, 4 failed."
gh issue comment 75 --body "From PR 7: the built image mshkn-recipe-img-<recipe id> is now kept as the legacy builder's layer cache and removed by DELETE /recipes and on build failure, so images are part of the recipe-side garbage collection this issue designs. Also: T10.8 creates and deletes two recipes per run and each leaves its template directory under checkpoint_local_dir/templates/, which nothing removes yet."
gh issue edit 65 --body "$(gh issue view 65 --json body --jq .body)

Update (PR 7): T10.1, T10.2 and T10.5 are implemented. Four remain: T8.6, T9.1, T11.2, T11.7 audit. Gate: 153 / 6 / 4."
```

---

## Self-review

**Spec coverage:** §5.1 → Task 2; §5.2 → Task 3; §5.3 → Task 4; §5.4 → Task 5; §6 helpers → Task 6; T7.9 → Task 6; the phase 3 change → Task 6; T10.1, T10.2 → Task 7; T10.3, T10.4 kept → Task 7; T10.5, T10.6 → Task 8; T10.7, T10.8 → Task 9; §7 documents → Task 10; §7 issues → Task 11 step 4; §8 validation → Task 11. The T10.8 size figure is printed (`df -m /`), per §2 item 5.

**Placeholders:** none. Every test has its code; every doc edit has its text. The PR body's angle-bracket fields are filled in Task 11 step 3 from the run's output.

**Type consistency:** `exec_command(..., timeout_seconds=)` (Tasks 6 to 9) matches Task 6's signature; `wait_for_recipe` returns `(info, elapsed)` everywhere it is unpacked; `_ok(result) -> str` and `_file_size(client, cid, path) -> int` are defined in Task 7 and used in Tasks 7 to 9; `recipe_image_tag` is defined in Task 5 and used only there; `BASE_IMAGE` is defined in `recipes.py` (Task 2) and re-imported by `base_volume.py` and the CLI; `FakeGuest.stream_timeouts` (Task 3) is what the flow test reads; the fakes in Task 4 carry both `exit_status` and `returncode`, which `_exit_code` reads.
