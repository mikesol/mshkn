# PR 1: Tooling, CI, and Hygiene — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the repository enforce its own quality bar: uv-only packaging, ruff and mypy strict over `src/` and `tests/`, three pytest tiers with E2E deselected by default, a GitHub Actions workflow, deploy and E2E scripts, and removal of dead code and the retired Telegram dev harness. Runtime behavior does not change.

**Architecture:** No source refactoring in this PR. The work is configuration, deletion, and mechanical cleanup of tests so that later PRs (foundations, host boundary, services, tests, docs) land on a tree that CI checks. Test tiers are assigned by directory via a root `conftest.py`, so a bare `pytest` runs unit tests only and `pytest -m e2e` runs the live suite.

**Tech Stack:** Python 3.12, uv 0.12, ruff 0.15, mypy 1.19, pytest 9 with pytest-asyncio 1.3 and pytest-cov, GitHub Actions with `astral-sh/setup-uv`.

**Spec:** `docs/superpowers/specs/2026-09-04-quality-overhaul-design.md` (§11 tests, §12 tooling and CI, §13 hygiene, §14 delivery step 1). Two deliberate reorderings from the spec's PR list: the `telegram/` and `skills/` removal (spec §13, listed under PR 6) happens here because CI lints the whole tree and those files would fail it; and the `uses=` / `timed()` E2E helper cleanup (spec §11, listed under PR 5) happens here because typing every E2E call site touches the same lines. One addition: `lz4` is removed from dependencies because nothing imports it.

## Global Constraints

- Python `>=3.12`; CI runs 3.12.
- uv is the only package manager. `poetry.lock` is deleted. `uv.lock` is committed and `uv lock --check` passes.
- Ruff `line-length = 100`, `target-version = "py312"`, the existing rule set (`E W F I N UP B A SIM TCH RUF PTH RET ARG ERA`), applied to `src/`, `tests/`, and `scripts/`. `ruff format` is enforced.
- mypy `strict = true` over `src` and `tests`.
- pytest markers `unit`, `flow`, `e2e` registered; `--strict-markers`; `addopts` deselects `e2e`.
- Every local validation command: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`.
- Live E2E on the replacement server is required before merge; if the server does not exist yet when this plan finishes, the PR stays open and unmerged until the baseline run is recorded.
- Commit messages end with the trailer block required by the session (Co-Authored-By and Claude-Session lines).
- Never merge; only open the PR and request merge authorization.

---

## File Structure

**Created**
- `.python-version` — pins `3.12` for uv.
- `.github/workflows/ci.yml` — the single CI workflow.
- `.pre-commit-config.yaml` — ruff check and format hooks.
- `tests/conftest.py` — assigns tier markers by directory.
- `tests/unit/__init__.py` — package marker for the moved unit tests.
- `scripts/deploy.sh` — ssh deploy to `$MSHKN_SERVER`.
- `scripts/e2e.sh` — push, deploy, orphan cleanup, account ensure, run E2E.

**Modified**
- `pyproject.toml` — PEP 621 metadata, dependency groups, ruff/mypy/pytest/coverage config.
- `uv.lock` — regenerated.
- `.gitignore` — drop telegram entries, add coverage artifacts.
- `CLAUDE.md` — remove Telegram section, replace validation and deploy commands, server reference via `MSHKN_SERVER`.
- `src/mshkn/main.py` — remove the `get_db` placeholder.
- `src/mshkn/vm/storage.py` — remove `init_thin_pool`, `create_base_volume`.
- `src/mshkn/vm/network.py` — remove `ensure_nat`.
- `src/mshkn/checkpoint/r2.py` — remove `download_checkpoint`.
- `src/mshkn/db.py` — remove `list_computers_by_account`.
- `tests/e2e/conftest.py` — remove `uses` parameters and `timed()`; type annotations.
- `tests/e2e/test_phase*.py` — remove `uses=[]` call sites and `"uses": []` bodies; lint, format, annotations.
- `tests/unit/test_*.py` — moved from `tests/`; lint, format, annotations; drop the `capability_cache` assertion.

**Deleted**
- `poetry.lock`, `deploy.sh`, `e2e_test.sh`, `skills-lock.json`
- `src/mshkn/checkpoint/delta.py`
- `tests/integration/` (whole directory)
- `telegram/`, `skills/` (copied to `../mshkn-devtools/` first)

---

### Task 1: Worktree and baseline

**Files:** none modified.

**Interfaces:**
- Produces: a branch `pr1-tooling-ci-hygiene` in a worktree at `../mshkn-pr1`, and a recorded baseline in the commit message of Task 11.

- [ ] **Step 1: Create the worktree**

Use the `superpowers:using-git-worktrees` skill. The resulting worktree must be at `../mshkn-pr1` on branch `pr1-tooling-ci-hygiene` from `main`. All later steps run inside that worktree. Create the venv there:

```bash
cd ../mshkn-pr1
uv sync
```

Expected: `.venv` created, `uv sync` exits 0.

- [ ] **Step 2: Record the baseline**

```bash
uv run ruff check src/ | tail -1
uv run ruff check tests/ | tail -1
uv run ruff format --check . 2>&1 | tail -1
uv run mypy src/ | tail -1
uv run pytest tests/ --ignore=tests/e2e --ignore=tests/integration -q 2>&1 | tail -1
```

Expected (as of 2026-09-04): src clean; tests "Found 60 errors"; "43 files would be reformatted"; mypy "Success: no issues found in 36 source files"; "109 passed". Write these five lines into `docs/superpowers/plans/2026-09-04-pr1-baseline.txt` so the final commit can cite them.

- [ ] **Step 3: Commit the baseline note**

```bash
git add docs/superpowers/plans/2026-09-04-pr1-baseline.txt
git commit -m "chore: record pre-PR1 quality baseline"
```

---

### Task 2: uv-only packaging

**Files:**
- Modify: `pyproject.toml`
- Create: `.python-version`
- Delete: `poetry.lock`
- Regenerate: `uv.lock`

**Interfaces:**
- Produces: `[dependency-groups] dev` with `pytest`, `pytest-asyncio`, `pytest-cov`, `mypy`, `ruff`; ruff/mypy/pytest/coverage configuration consumed by every later task; `uv run <tool>` as the invocation form.

- [ ] **Step 1: Replace `pyproject.toml` in full**

```toml
[project]
name = "mshkn"
version = "0.1.0"
description = "Computers that fork: disposable cloud computers for AI agents"
readme = "README.md"
license = "MIT"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.34",
    "aiosqlite>=0.21",
    "asyncssh>=2.18",
    "httpx>=0.28",
    "sse-starlette>=2.1",
    "pydantic>=2.10",
    "prometheus-client>=0.21",
    "starlark-go>=1.0.1,<2",
]

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "pytest-cov>=6.0",
    "mypy>=1.13",
    "ruff>=0.8",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/mshkn"]

[tool.ruff]
target-version = "py312"
line-length = 100
extend-exclude = [".worktrees", "docs"]

[tool.ruff.lint]
select = [
    "E",     # pycodestyle errors
    "W",     # pycodestyle warnings
    "F",     # pyflakes
    "I",     # isort
    "N",     # pep8-naming
    "UP",    # pyupgrade
    "B",     # flake8-bugbear
    "A",     # flake8-builtins
    "SIM",   # flake8-simplify
    "TCH",   # flake8-type-checking
    "RUF",   # ruff-specific
    "PTH",   # flake8-use-pathlib
    "RET",   # flake8-return
    "ARG",   # flake8-unused-arguments
    "ERA",   # eradicate (commented-out code)
]

[tool.ruff.lint.per-file-ignores]
# pytest fixtures are frequently requested only for their side effects
"tests/**" = ["ARG001", "ARG002"]

[tool.mypy]
files = ["src", "tests"]
python_version = "3.12"
strict = true
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true
disallow_incomplete_defs = true
check_untyped_defs = true
disallow_any_generics = true
no_implicit_reexport = true

[[tool.mypy.overrides]]
module = ["asyncssh.*", "sse_starlette.*"]
ignore_missing_imports = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
testpaths = ["tests"]
addopts = "-m 'not e2e' --strict-markers"
markers = [
    "unit: pure tests with no I/O beyond temp files",
    "flow: full ASGI app against the in-memory fake host and a temp SQLite database",
    "e2e: live server with real Firecracker VMs; requires MSHKN_API_URL",
]

[tool.coverage.run]
source = ["mshkn"]
branch = true

[tool.coverage.report]
show_missing = true
skip_covered = true
```

- [ ] **Step 2: Pin the interpreter and drop the Poetry lock**

```bash
echo "3.12" > .python-version
git rm -q poetry.lock
```

- [ ] **Step 3: Regenerate the lock and sync**

```bash
uv lock
uv sync
uv lock --check
```

Expected: `uv lock` rewrites `uv.lock` (removing `requests`, `lz4`, and Poetry-only metadata); `uv sync` installs `pytest-cov`; `uv lock --check` prints nothing and exits 0.

- [ ] **Step 4: Verify the tools still run under uv**

```bash
uv run python -c "import mshkn.main; print('import ok')"
uv run ruff --version
uv run mypy --version
uv run pytest --co -q tests/test_health.py | tail -1
```

Expected: `import ok`; ruff and mypy print versions; pytest collects 1 test. If `pytest` errors with `'unit' not found in markers` or similar, the `markers` table in Step 1 is malformed; fix it before continuing.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .python-version uv.lock
git commit -m "build: make uv the only package manager

Pure PEP 621 metadata with a dev dependency group, pytest-cov added,
unused requests and lz4 dependencies dropped, poetry.lock removed.
Ruff, mypy, pytest, and coverage configuration consolidated; mypy now
covers tests; pytest registers unit/flow/e2e markers and deselects
e2e by default."
```

---

### Task 3: Test tiers by directory

**Files:**
- Create: `tests/conftest.py`, `tests/unit/__init__.py`
- Move: `tests/test_*.py` → `tests/unit/`

**Interfaces:**
- Produces: every test under `tests/unit/` carries `@pytest.mark.unit`, `tests/flow/` → `flow` (directory created in PR 3), `tests/e2e/` → `e2e`. `uv run pytest` runs unit tests only; `uv run pytest -m e2e` runs the live suite.

- [ ] **Step 1: Write the tier conftest**

`tests/conftest.py`:

```python
"""Assign a tier marker to every test based on its directory.

tests/unit/  -> unit   (pure; no I/O beyond temp files)
tests/flow/  -> flow   (real ASGI app, fake host, temp SQLite)
tests/e2e/   -> e2e    (live server; deselected by default via pyproject addopts)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable

_TIERS = ("unit", "flow", "e2e")


def pytest_collection_modifyitems(config: pytest.Config, items: Iterable[pytest.Item]) -> None:
    tests_root = config.rootpath / "tests"
    for item in items:
        try:
            tier = item.path.relative_to(tests_root).parts[0]
        except ValueError:
            continue
        if tier in _TIERS:
            item.add_marker(getattr(pytest.mark, tier))
```

- [ ] **Step 2: Move the unit tests**

```bash
mkdir -p tests/unit
touch tests/unit/__init__.py
git mv tests/test_auth.py tests/test_checkpoint_label_filter.py tests/test_checkpoint_parent.py \
       tests/test_db.py tests/test_exclusive_restore.py tests/test_exec_on_create.py \
       tests/test_firecracker.py tests/test_health.py tests/test_ingress.py tests/test_merge.py \
       tests/test_metrics.py tests/test_models.py tests/test_network.py tests/test_recipe_builder.py \
       tests/test_recipe_db.py tests/test_self_destruct.py tests/test_ssh.py \
       tests/test_vm_limit.py tests/test_vm_manager.py tests/unit/
git add tests/conftest.py tests/unit/__init__.py
```

- [ ] **Step 3: Verify tiering**

```bash
uv run pytest -q 2>&1 | tail -2
uv run pytest -m unit --co -q 2>&1 | tail -1
uv run pytest -m e2e --co -q 2>&1 | tail -1
```

Expected: first command "109 passed" and a line like "157 deselected"; second "109 tests collected"; third "157 tests collected" (collection only; do not run e2e). If the first line shows e2e tests running or erroring on connection, `addopts` from Task 2 is not being applied; confirm you are in the worktree root.

- [ ] **Step 4: Commit**

```bash
git commit -m "test: tier tests by directory (unit/flow/e2e) and deselect e2e by default"
```

---

### Task 4: Remove dead code and legacy files

**Files:**
- Delete: `src/mshkn/checkpoint/delta.py`, `tests/integration/`, `e2e_test.sh`
- Modify: `src/mshkn/vm/storage.py`, `src/mshkn/vm/network.py`, `src/mshkn/checkpoint/r2.py`, `src/mshkn/db.py`, `src/mshkn/main.py`, `tests/unit/test_db.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `storage.py` exports only `pool_create_snap`, `create_snapshot`, `remove_volume`, `mount_volume`, `umount_volume`; `network.py` exports `slot_to_ip`, `slot_to_mac`, `slot_to_tap`, `create_tap`, `destroy_tap`; `r2.py` exports `upload_checkpoint`, `delete_checkpoint_r2`.

- [ ] **Step 1: Prove each symbol is unreferenced**

```bash
for sym in export_disk_delta import_disk_delta init_thin_pool create_base_volume ensure_nat download_checkpoint get_db list_computers_by_account; do
  printf "%-26s %s\n" "$sym" "$(grep -rn "\b$sym\b" src tests --include='*.py' | grep -v "def $sym" | grep -vc "_download_checkpoint_snapshot")"
done
```

Expected: every count is `0`. If any is non-zero, stop and inspect; do not delete a referenced symbol.

- [ ] **Step 2: Delete files**

```bash
git rm -q src/mshkn/checkpoint/delta.py e2e_test.sh
git rm -rq tests/integration
```

- [ ] **Step 3: Remove functions**

In `src/mshkn/vm/storage.py` delete `init_thin_pool` (the whole `async def init_thin_pool(...)` through its final `logger.info(...)` line) and `create_base_volume` (through its `logger.info(...)` line). After deletion the module begins:

```python
from __future__ import annotations

import asyncio
import logging

from mshkn.shell import ShellError, run

logger = logging.getLogger(__name__)


async def pool_create_snap(
```

The `if TYPE_CHECKING: from pathlib import Path` block is no longer needed; remove it and the `TYPE_CHECKING` import.

In `src/mshkn/vm/network.py` delete `ensure_nat` (from `async def ensure_nat(` to the end of the file).

In `src/mshkn/checkpoint/r2.py` delete `download_checkpoint` (from `async def download_checkpoint(` to the end of the file).

In `src/mshkn/db.py` delete `list_computers_by_account` (from `async def list_computers_by_account(` through the closing `]` of its return list).

In `src/mshkn/main.py` delete:

```python
async def get_db() -> aiosqlite.Connection:
    """Dependency placeholder -- overridden in tests, set in lifespan for prod."""
    raise RuntimeError("DB not initialized")
```

and keep the `import aiosqlite` line (still used by the lifespan).

- [ ] **Step 4: Drop the Nix-era assertion**

In `tests/unit/test_db.py`, `test_migrations_apply`, remove the line:

```python
    assert "capability_cache" in tables
```

- [ ] **Step 5: Verify**

```bash
uv run ruff check src/
uv run mypy src/
uv run pytest -q 2>&1 | tail -1
```

Expected: "All checks passed!"; "Success: no issues found in 35 source files"; "109 passed".

- [ ] **Step 6: Commit**

```bash
git add -A src tests e2e_test.sh
git commit -m "chore: remove dead code and legacy test scripts

delta.py, init_thin_pool, create_base_volume, ensure_nat,
download_checkpoint, list_computers_by_account, and the get_db
placeholder had no callers. tests/integration and e2e_test.sh
predate the recipe system and were never run."
```

---

### Task 5: Retire the Telegram dev harness

**Files:**
- Delete: `telegram/`, `skills/`, `skills-lock.json`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `../mshkn-devtools/` (outside git) holding a copy of `telegram/` and `skills/` including the untracked `.env`, `offset.txt`, and jsonl files.

- [ ] **Step 1: Copy out, then remove**

```bash
mkdir -p ../mshkn-devtools
cp -a telegram skills skills-lock.json ../mshkn-devtools/
ls ../mshkn-devtools/telegram/.env ../mshkn-devtools/telegram/bridge.py ../mshkn-devtools/skills/telegram-agent/SKILL.md
git rm -rq telegram skills skills-lock.json
rm -rf telegram skills
git status --short | head
```

Expected: the three `ls` paths exist; `git status` shows deletions only.

- [ ] **Step 2: Replace `.gitignore`**

```
.env
.venv/
__pycache__/
*.pyc
.mypy_cache/
.ruff_cache/
.pytest_cache/
.coverage
htmlcov/
*.egg-info/
dist/
.agents/
.claude/
.worktrees/
.superpowers/
```

- [ ] **Step 3: Commit**

```bash
git add -A .gitignore telegram skills skills-lock.json
git commit -m "chore: move Telegram bridge and telegram-agent skill out of the repo

Superseded by Claude remote control. Copied to ../mshkn-devtools/
before removal."
```

---

### Task 6: E2E helper cleanup

**Files:**
- Modify: `tests/e2e/conftest.py`, `tests/e2e/test_phase*.py`

**Interfaces:**
- Produces: `create_computer(client, recipe_id=None)` and `managed_computer(client, recipe_id=None)` with no `uses` parameter; no `timed()`.

- [ ] **Step 1: Remove `uses` and `timed()` from the helpers**

In `tests/e2e/conftest.py` replace the `create_computer` definition with:

```python
async def create_computer(
    client: httpx.AsyncClient,
    recipe_id: str | None = None,
) -> str:
    """Create a computer, return computer_id."""
    body: dict[str, object] = {}
    if recipe_id:
        body["recipe_id"] = recipe_id
    resp = await client.post("/computers", json=body)
    resp.raise_for_status()
    return resp.json()["computer_id"]
```

Replace `managed_computer` with:

```python
@asynccontextmanager
async def managed_computer(
    client: httpx.AsyncClient,
    recipe_id: str | None = None,
) -> AsyncIterator[str]:
    """Context manager that creates and destroys a computer."""
    comp_id = await create_computer(client, recipe_id=recipe_id)
    try:
        yield comp_id
    finally:
        await destroy_computer(client, comp_id)
```

Delete the `timed()` function entirely (from `def timed()` through its `raise NotImplementedError(...)` line).

- [ ] **Step 2: Remove `uses` at every call site**

```bash
sed -i -E 's/, uses=\[\]//g; s/\(client, uses=\[\]\)/(client)/g; s/\(long_client, uses=\[\]\)/(long_client)/g' tests/e2e/test_phase*.py
sed -i -E 's/"uses": \[\], //g; s/\{"uses": \[\]\}/{}/g' tests/e2e/test_phase*.py
grep -rn "uses" tests/e2e/ || echo "no uses left"
```

Expected: the final grep prints `no uses left`. If it prints lines, fix each by hand: remove the `uses` argument or key, keeping everything else on the line.

- [ ] **Step 3: Verify collection**

```bash
uv run pytest -m e2e --co -q 2>&1 | tail -1
```

Expected: "157 tests collected". A `TypeError` or `SyntaxError` during collection means a sed left a dangling comma; fix it.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e
git commit -m "test(e2e): drop the ignored uses parameter and the timed() stub"
```

---

### Task 7: Lint and format the test suite

**Files:**
- Modify: `tests/**/*.py`

**Interfaces:**
- Produces: `uv run ruff check .` and `uv run ruff format --check .` both clean.

- [ ] **Step 1: Apply automatic fixes**

```bash
uv run ruff format .
uv run ruff check . --fix
uv run ruff check . --fix --unsafe-fixes
uv run ruff check . 2>&1 | tail -3
```

Expected: the remaining error count is small (roughly 10: `E741`, `E501`, `B905`, `A002`, `B007`). Ruff never introduces behavior changes here except for `SIM105` (`try/except: pass` → `contextlib.suppress`), which is equivalent.

- [ ] **Step 2: Fix `E741` ambiguous names**

```bash
sed -i -E 's/\bl\.strip\(\) for l in\b/line.strip() for line in/g; s/ if l\.strip\(\)\]/ if line.strip()]/g' tests/e2e/test_phase*.py
uv run ruff check . --select E741
```

Expected: "All checks passed!".

- [ ] **Step 3: Fix the remaining errors by hand**

Run `uv run ruff check . --output-format concise` and fix each:

- `E501` line too long: split the string literal or expression across lines; do not shorten the message text.
- `B905` `zip()` without `strict=`: add `strict=True` when both iterables must be the same length (they are, in these tests), else `strict=False`.
- `A002` argument shadows a builtin (`id`, `input`, ...): rename the parameter and its uses inside the function.
- `B007` unused loop variable: rename to `_name`.
- `F401` unused import that `--fix` could not remove (inside `TYPE_CHECKING` or `__init__`): delete it.
- `ERA001` commented-out code: delete the comment.

After each fix, rerun `uv run ruff check .` until "All checks passed!".

- [ ] **Step 4: Verify nothing broke**

```bash
uv run ruff format --check .
uv run pytest -q 2>&1 | tail -1
uv run pytest -m e2e --co -q 2>&1 | tail -1
```

Expected: "75 files already formatted" (or the current file count); "109 passed"; "157 tests collected".

- [ ] **Step 5: Commit**

```bash
git add -A tests src
git commit -m "style: lint and format the test suite under the project ruff rules"
```

---

### Task 8: Type-check the test suite

**Files:**
- Modify: `tests/**/*.py`

**Interfaces:**
- Produces: `uv run mypy` (which now covers `src` and `tests` via `files` in pyproject) reports "Success".

- [ ] **Step 1: Add `-> None` to every test function**

```bash
uv run ruff check tests --select ANN201,ANN202 --fix --unsafe-fixes
grep -rn "def test_" tests | grep -vc "\-> None"
```

Expected: the count is `0`. Ruff's `ANN` fix adds `-> None` to functions with no `return <value>`; any test it skipped returns a value from a helper and needs the annotation by hand. (`ANN` is selected only for this one-off command; it is not added to the project rule set.)

- [ ] **Step 2: Annotate fixture parameters in E2E tests**

```bash
sed -i -E 's/\(self, client\)/(self, client: httpx.AsyncClient)/g; s/\(self, long_client\)/(self, long_client: httpx.AsyncClient)/g' tests/e2e/test_phase*.py
for f in tests/e2e/test_phase*.py; do
  grep -q "httpx.AsyncClient" "$f" && ! grep -q "^import httpx" "$f" && sed -i '0,/^from __future__ import annotations$/s//from __future__ import annotations\n\nimport httpx/' "$f"
done
uv run ruff check tests/e2e --fix
```

Expected: every file that uses the annotation imports `httpx`; ruff's isort pass places the import correctly.

- [ ] **Step 3: Run mypy and fix what remains**

```bash
uv run mypy 2>&1 | tail -20
```

Fix each reported error in place. The categories you will meet and the fix for each:

- `Function is missing a type annotation for one or more arguments`: annotate the parameter. Fixtures: `tmp_path: Path`, `monkeypatch: pytest.MonkeyPatch`, `client: httpx.AsyncClient`. Helpers taking a DB: `db: aiosqlite.Connection`.
- `Returning Any from function declared to return "X"`: the value comes from `resp.json()`; assign to a typed local, e.g. `body: dict[str, Any] = resp.json()`, and return the typed field.
- `Need type annotation for "x"`: add the annotation, e.g. `timings: list[float] = []`.
- `"object" has no attribute ...` on `resp.json()[...]` chains: annotate the intermediate as `dict[str, Any]` or `list[dict[str, Any]]`.
- `Argument 1 to "..." has incompatible type "_FakeConfig"`: leave the `app.state` assignments as they are (`app.state` is `Any`); mypy only complains where a typed function receives the fake; cast with `typing.cast` at the call.
- `Untyped decorator makes function untyped` for `@pytest.fixture` on async fixtures: ensure the fixture's own signature is fully annotated (return `AsyncIterator[httpx.AsyncClient]`).

Repeat until:

```bash
uv run mypy
```

Expected: "Success: no issues found in NN source files" where NN is around 70.

- [ ] **Step 4: Verify tests still pass and E2E still collects**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q 2>&1 | tail -1 && uv run pytest -m e2e --co -q 2>&1 | tail -1
```

Expected: both ruff commands clean; "109 passed"; "157 tests collected".

- [ ] **Step 5: Commit**

```bash
git add -A tests
git commit -m "test: type-annotate the test suite; mypy strict now covers tests"
```

---

### Task 9: CI workflow and pre-commit

**Files:**
- Create: `.github/workflows/ci.yml`, `.pre-commit-config.yaml`

**Interfaces:**
- Produces: a required check named `ci / check` on every PR and push to `main`.

- [ ] **Step 1: Write the workflow**

`.github/workflows/ci.yml`:

```yaml
name: ci

on:
  push:
    branches: [main]
  pull_request:

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  check:
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v6
        with:
          enable-cache: true

      - name: Install Python
        run: uv python install

      - name: Sync dependencies
        run: uv sync --frozen

      - name: Lock file is current
        run: uv lock --check

      - name: Ruff lint
        run: uv run ruff check .

      - name: Ruff format
        run: uv run ruff format --check .

      - name: mypy
        run: uv run mypy

      - name: Unit and flow tests
        run: uv run pytest --cov --cov-report=term-missing
```

`uv python install` with no argument reads `.python-version` from Task 2.

- [ ] **Step 2: Write the pre-commit config**

`.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.15.5
    hooks:
      - id: ruff-check
        args: [--fix]
      - id: ruff-format
```

- [ ] **Step 3: Run the exact CI commands locally**

```bash
uv sync --frozen && uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov --cov-report=term-missing 2>&1 | tail -15
```

Expected: every command exits 0; the coverage table prints with a TOTAL line. Note the TOTAL percentage; it is the starting point for the floor set in PR 5.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml .pre-commit-config.yaml
git commit -m "ci: add GitHub Actions workflow (uv, ruff, mypy, pytest with coverage)"
```

---

### Task 10: Deploy and E2E scripts, CLAUDE.md

**Files:**
- Create: `scripts/deploy.sh`, `scripts/e2e.sh`
- Delete: `deploy.sh`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: env `MSHKN_SERVER` (`root@<ip>`), optional `MSHKN_API_URL` (default `http://<ip>:8000`).
- Produces: `scripts/e2e.sh [pytest args]` as the single E2E entry point named in CLAUDE.md.

- [ ] **Step 1: Write `scripts/deploy.sh`**

```bash
#!/usr/bin/env bash
# Deploy the currently pushed branch to the live server and restart the service.
# Usage: MSHKN_SERVER=root@<ip> scripts/deploy.sh
set -euo pipefail

: "${MSHKN_SERVER:?set MSHKN_SERVER to root@<ip> of the live KVM server}"

ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$MSHKN_SERVER" bash -s <<'REMOTE'
set -euo pipefail
cd /opt/mshkn
git pull --ff-only
~/.local/bin/uv sync --frozen
systemctl restart mshkn litestream
sleep 2
systemctl is-active mshkn litestream
REMOTE

echo "deployed to $MSHKN_SERVER"
```

- [ ] **Step 2: Write `scripts/e2e.sh`**

```bash
#!/usr/bin/env bash
# Push, deploy, clean orphaned VM resources, ensure the test account, run the live E2E suite.
# Usage: MSHKN_SERVER=root@<ip> scripts/e2e.sh [extra pytest args]
set -euo pipefail

: "${MSHKN_SERVER:?set MSHKN_SERVER to root@<ip> of the live KVM server}"
SERVER_IP="${MSHKN_SERVER#*@}"
API_URL="${MSHKN_API_URL:-http://${SERVER_IP}:8000}"
API_KEY="${MSHKN_API_KEY:-mk-test-key-2026}"
HERE="$(cd "$(dirname "$0")" && pwd)"

git push
"$HERE/deploy.sh"

# Stop the service, kill leftover VMs, remove orphaned taps and computer/staging thin
# devices (checkpoint and recipe volumes are persistent and must survive), then start.
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$MSHKN_SERVER" bash -s <<REMOTE
set -euo pipefail
systemctl stop mshkn litestream
pkill -x firecracker || true
sleep 1
for tap in \$(ip -o link show type tun | awk -F': ' '{print \$2}' | grep -E '^tap[0-9]+\$' || true); do
  ip link del "\$tap" || true
done
for vol in \$(dmsetup ls --target thin | awk '{print \$1}' | grep -E '^mshkn-(comp-|restore-staging)' || true); do
  dmsetup remove "\$vol" || true
done
sqlite3 /opt/mshkn/mshkn.db "INSERT OR IGNORE INTO accounts (id, api_key, vm_limit) VALUES ('acct-mike', '${API_KEY}', 20);"
systemctl start mshkn litestream
for _ in \$(seq 1 20); do
  curl -fsS http://localhost:8000/health >/dev/null 2>&1 && break
  sleep 0.5
done
curl -fsS http://localhost:8000/health
echo
REMOTE

echo "running E2E against $API_URL"
MSHKN_API_URL="$API_URL" MSHKN_API_KEY="$API_KEY" uv run pytest tests/e2e -m e2e -v --tb=short "$@"
```

(The account insert uses `sqlite3` until the accounts CLI lands in PR 4; PR 4's plan replaces that line.)

- [ ] **Step 3: Make executable, remove the old script**

```bash
chmod +x scripts/deploy.sh scripts/e2e.sh
git rm -q deploy.sh
bash -n scripts/deploy.sh && bash -n scripts/e2e.sh && echo "syntax ok"
uv run ruff check scripts/ && echo "ruff ok (no python)"
```

Expected: `syntax ok`; ruff reports nothing for a directory with no Python files.

- [ ] **Step 4: Update CLAUDE.md**

Delete the entire `## Telegram Bridge` section (from the heading through the last bullet under `### Responding`).

Replace the `## Server reference` section with:

```markdown
## Server reference

The live E2E server is a dedicated KVM host set up from `DEPLOY.md`. Export its
address once per shell:

    export MSHKN_SERVER=root@<ip>

- **Deploy**: `scripts/deploy.sh`
- **E2E**: `scripts/e2e.sh` (pushes, deploys, cleans orphaned VM resources, ensures the
  test account, runs `pytest tests/e2e -m e2e`)
- **Service**: `ssh $MSHKN_SERVER systemctl {restart,status,stop} mshkn`
- **Logs**: `ssh $MSHKN_SERVER journalctl -u mshkn --since '5 min ago' --no-pager`
- **Test account**: `acct-mike` / `mk-test-key-2026`
```

In `## Standing rules` replace the **Validate locally first** bullet with:

```markdown
- **Validate locally first**: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`. This is exactly what CI runs. Always use the project venv via `uv run`.
```

Replace the **E2E tests on live infra** bullet's address text so it reads:

```markdown
- **E2E tests on live infra are the source of truth.** After deploying, run `scripts/e2e.sh`. Never accept regressions — if a test that passed before now fails, that's a real problem. Fix it or stop and discuss.
```

Replace the **Deploy workflow** bullet with:

```markdown
- **Deploy workflow**: commit → `scripts/e2e.sh` (it pushes, deploys, cleans orphan dm-thin volumes, tap devices, and firecracker processes, and recreates the test account if the DB was reset).
```

Remove the sentence "Always use `.venv` for poetry, pytest, python, and formatters." from that section (it is now covered by the validate bullet).

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy.sh scripts/e2e.sh deploy.sh CLAUDE.md
git commit -m "chore: add deploy and e2e scripts parameterized by MSHKN_SERVER; update CLAUDE.md

Removes the Telegram bridge instructions (superseded by Claude remote
control) and the hard-coded server address."
```

---

### Task 11: Final verification and pull request

**Files:** none new.

- [ ] **Step 1: Full local validation**

Use the `superpowers:verification-before-completion` skill. Run:

```bash
uv sync --frozen && uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov 2>&1 | tail -5
git status --short
```

Expected: all exit 0; "109 passed"; `git status` clean.

- [ ] **Step 2: Push and open the PR**

```bash
git push -u origin pr1-tooling-ci-hygiene
gh pr create --title "PR 1: tooling, CI, and hygiene" --body-file - <<'EOF'
Part 1 of 6 of the quality overhaul (spec: docs/superpowers/specs/2026-09-04-quality-overhaul-design.md, plan: docs/superpowers/plans/2026-09-04-pr1-tooling-ci-hygiene.md).

**What this does**
Makes uv the only package manager, extends ruff and mypy strict to tests, tiers tests by directory with E2E deselected by default, adds a GitHub Actions workflow and pre-commit config, replaces the hard-coded deploy script with MSHKN_SERVER-parameterized deploy and E2E scripts, and removes dead code (delta.py, unused storage/network/r2/db helpers), legacy test scripts, and the retired Telegram dev harness. No runtime behavior changes.

**Design alignment**
- Spec §12 (tooling and CI): implemented as written.
- Spec §11 (test tiers): markers by directory; E2E helper `uses`/`timed()` cleanup pulled forward from PR 5 because typing every call site touched the same lines.
- Spec §13 (hygiene): dead code removal as listed; Telegram/skills removal pulled forward from PR 6 because CI lints the whole tree. `lz4` additionally removed as an unused dependency.

**Validation performed**
- Baseline before: <paste docs/superpowers/plans/2026-09-04-pr1-baseline.txt>
- After: `uv run ruff check .` clean, `uv run ruff format --check .` clean, `uv run mypy` Success over src and tests, `uv run pytest --cov` 109 passed, coverage TOTAL <n>%.
- CI: <link to green run>
- Live E2E: <either "scripts/e2e.sh: N passed, M skipped, 0 failed on <ip>" or "PENDING: replacement server not yet provisioned; this PR is not to be merged until the baseline E2E run is recorded here">

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01CPKyFZiT4pPi4v5gkph5KZ
EOF
```

Fill in the three `<...>` fields before submitting; the command above is a template for the body, not a literal to paste with placeholders left in.

- [ ] **Step 3: Wait for CI**

```bash
gh pr checks --watch
```

Expected: `ci / check` passes. If it fails, read the log (`gh run view --log-failed`), fix in the worktree, commit, push, and watch again.

- [ ] **Step 4: Live E2E**

If `MSHKN_SERVER` exists: run `scripts/e2e.sh` and paste the summary into the PR body (`gh pr edit --body-file`). Compare against the previous recorded baseline; any test that passed before and fails now blocks the PR.

If the server does not exist yet: leave the PENDING line in the PR body and stop. Do not request merge.

- [ ] **Step 5: Triage bot reviews**

Follow the "How to handle PR reviews" section of CLAUDE.md: reply to every comment, resolve every thread, fix only what is actually wrong.

- [ ] **Step 6: Request merge authorization**

Report to the owner with the CI link and the E2E summary. Do not merge.

---

## Self-review

**Spec coverage (PR 1 scope):** §12 tooling (Task 2), CI (Task 9), pre-commit (Task 9), scripts (Task 10) — covered. §11 tiers and markers (Task 3), E2E helper cleanup (Task 6), tests linted and typed (Tasks 7, 8) — covered; the `manifest_hash` → `recipe_id` E2E assertion change stays in PR 5 because it depends on PR 4's API change. §13 dead code (Task 4), Telegram/skills removal (Task 5), `.gitignore` (Task 5), CLAUDE.md validation command and server reference (Task 10) — covered; README, ARCHITECTURE, plans index, DEPLOY.md remain PR 6 except for corrections discovered while setting up the replacement server, which land in whichever PR is open at the time. Coverage floor: measured here (Task 9 Step 3), enforced in PR 5.

**Placeholder scan:** the only angle-bracket fields are in the PR body template and are explicitly instructed to be filled before submission.

**Type consistency:** helper signatures in Task 6 (`create_computer(client, recipe_id=None)`, `managed_computer(client, recipe_id=None)`) match the sed rewrites in the same task; Task 8's `httpx.AsyncClient` annotation matches the fixtures in `tests/e2e/conftest.py`.
