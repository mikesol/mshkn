# The Generative-Agent Workload Proof

**Date:** 2026-09-07
**Status:** Approved (design), awaiting spec review
**Issue:** #76 (brainstorm; `spec-change`)
**Scope:** Two PRs. PR 7a makes the bare base the same filesystem recipes are built from. PR 7 proves, on the live host, the loop a generative agent will live in: write a recipe, build it, read the failure, fix it, run real tools, checkpoint, fork, diverge, pick. Three small product changes ride with PR 7 because the proof cannot be written without them.

## 1. Why

mshkn's purpose is a generative agent that grows its own surface area by writing recipes (task-specific images) and the programs those computers run. The six-PR quality overhaul (#60 to #64, #72) proved the primitives; the live suite proves nothing about the loop. Every recipe in it is trivial, the longest exec is bounded by a 60-second wall the agent does not know about, no test checkpoints a computer with a live server, no test builds a toolchain-sized image, and no test submits a broken recipe, reads the build log and resubmits. Three of the seven `Not implemented` tests in #65 are the old T10 demos, written before this goal existed.

Studying the code for this brainstorm found four things the loop would trip over on day one:

1. **The bare base is a Nix-era artifact.** Thin volume 0 is written from `scripts/build-rootfs.sh`: a debootstrap image that deletes apt and dpkg, installs shims so `apt-get`, `pip` and `npm` exit 1 with a JSON error telling the agent to use the removed `uses` manifest, boots a minimal `mshkn-init` instead of systemd, and pre-creates `/nix`. Recipe computers are exports of the `mshkn-base` Docker image with systemd as PID 1 and a working apt. The config field `base_rootfs_path` is read by nothing.
2. **The streaming exec kills every command at 60 seconds**, and reports the killed command as exit 0: `SshGuest._pump` ends with `process.exit_status or 0`, and a signal-terminated process has no exit status. A `pip install` in the foreground cannot finish, and the agent is told it did.
3. **Rebuilds are cold.** The host's Docker (29.1.3, no buildx plugin) uses the deprecated legacy builder, whose cache is the image layer chain, and `RecipeService.build` removes its image after export. "Agent appends one line, Docker rebuilds only that layer" is what the recipe design promised and not what happens.
4. **Nothing checks the base image** (#73). A Dockerfile from any base boots by accident or fails at create time with an SSH timeout.

## 2. Decisions

The issue asked seven questions. Answers, with the two discovered decisions:

| # | Question | Decision |
|---|---|---|
| 1 | The loop to prove | Eight T10 tests (§6) plus T7.9 for the exec limit. |
| 2 | Which T10 tests survive | T10.3 and T10.4 stay as they are. T10.1 (Next.js) is replaced by the ffmpeg loop; the node toolchain in T10.8 is the large-install case. T10.2 (pandas) becomes apt and pip on a bare computer. T10.5 becomes a scripted, deterministic agent loop; the "works on the cheapest model" claim leaves the definition of done. |
| 3 | Base-image rule (#73) | Enforce, at `POST /recipes`, as a 422 naming the rule and the offending image. The rule is on the **last** `FROM`, the stage that is exported (§5.1). |
| 4 | Incremental growth without `FROM <recipe>` | The agent owns its Dockerfile text and appends. T10.8 asserts the appended-layer rebuild finishes in under half the cold build. No recipe layering. Requires §5.4. |
| 5 | Budgets | Every recipe reaches `ready` within the documented 600-second build cap. Exec runs within the `timeout_seconds` the caller sets (at most 600). Image size is printed, not asserted. The suite grows from about 14 to about 26 minutes. |
| 6 | What the agent needs to see | #58 (exec output of ephemeral turns) is its own PR after #70. None of the nine tests needs it: every exec is on a live computer whose output comes back in the response. |
| 7 | Order | PR 7a, PR 7, #70, #58, then the first real agent. #74 and #75 are designed when a real agent hits them. |
| A | The bare base | One base: volume 0 becomes the export of `mshkn-base`, written by a CLI command through the same code path recipes use. Its own PR (7a), before the proof, with its own live gate. |
| B | Exit after a kill | A signal-terminated process reports 128 + signal number (137 for the timeout's SIGKILL); 255 when neither status nor signal is known. |
| C | Recipe image retention | The successful image is kept, tagged by recipe id, and removed with the recipe or on build failure. |
| D | The other four #65 tests | T8.6, T9.1, T11.2 and the T11.7 audit check stay in #65. PR 7's live gate is 153 passed, 6 skipped, 4 failed, those four named. |

## 3. Sequencing

1. **PR 7a, one base** (§4). Live gate unchanged: 144 / 6 / 7, the same seven.
2. **PR 7, the proof** (§5, §6). Live gate: 153 / 6 / 4.
3. #70, the destroy-versus-reaper teardown race, as its own PR; the workload tests are the ones that will show whether it bites.
4. #58, exec log retention, as its own PR.
5. The first real agent.

Each PR is planned in `docs/superpowers/plans/` and executed by subagent-driven development, like PRs 1 to 6.

## 4. PR 7a: one base

### 4.1 What changes

`python -m mshkn base-volume` writes thin volume 0:

1. Refuses if the service is active (`systemctl is-active --quiet mshkn` succeeds through the injected shell runner), because a concurrent create would snapshot a half-written origin.
2. Builds the `mshkn-base` image: writes `Dockerfile.mshkn-base` and the public half of `Config.ssh_key_path` into a temporary context and runs the same `docker build` command shape as a recipe build (memory and CPU limits, ten-minute timeout).
3. Exports a container from the image and writes the tar into the already-active `mshkn-base` device (volume 0, activated by `scripts/mshkn-pool-up`) through the shared image-to-volume step (§4.2): mkfs, mount, untar, post-process, unmount.
4. Deletes the bare template row in `snapshot_templates` and the directory `checkpoint_local_dir/templates/bare`, since that template was booted from the old base.

Options: `--dockerfile PATH` (default: the repository's `Dockerfile.mshkn-base`, found relative to the package), `--image TAG` (default `mshkn-base`), `--device NAME` (default `mshkn-base`).

### 4.2 The shared step

`RecipeService.build` currently does docker create, export, rm, then mkfs, mount, untar and `_post_process_rootfs` inline. That sequence moves into one module-level coroutine in `src/mshkn/services/recipes.py`:

```python
async def write_image_to_volume(
    *, run: RunFn, blocks: BlockStore, config: Config,
    image_tag: str, volume_name: str, work_dir: Path,
) -> None:
    """docker create/export/rm the image, mkfs the active device, untar, post-process."""
```

`RecipeService.build` calls it between `activate` and `deactivate`; the CLI calls it on the already-active base device. The recipe service tests keep asserting the same command chain through it.

### 4.3 What goes

- `scripts/build-rootfs.sh`: debootstrap, the purity shims, the apt removal, `mshkn-init`, the `/nix` directories, the fcnet duplicate.
- `Config.base_rootfs_path` and its row in the architecture doc's configuration table.
- `DEPLOY.md` §5 (rootfs), the `dd`/`resize2fs`/`e2fsck` lines of §6, and §7 (base image), replaced by one section: bring the pool up, then run `python -m mshkn base-volume` with the environment loaded. `/opt/firecracker/rootfs.ext4` is no longer produced or needed.
- README's mention of `build-rootfs.sh` in the layout block.

### 4.4 Consequences

- Systemd is PID 1 on bare computers too. Cold boots gain roughly a quarter second. Creates at default resources restore from the template, so the phase 1 latency gates do not move; the one cold boot in the suite (T5.6, custom RAM) has no latency assertion.
- Bare computers have a working apt, and `pip`/`npm` are absent rather than shimmed, so their errors are the real "command not found".
- Thin snapshots are independent of their origin once taken: rewriting volume 0 does not touch existing checkpoint or recipe volumes. Old checkpoints keep whatever init they were taken with.
- Bare and recipe filesystems are produced by identical code, so a recipe difference is a Dockerfile difference and nothing else.

### 4.5 Tests

- Unit: the CLI over the fake block store and a shell recorder asserts the sequence (refusal while active; image build with the key in context; create, export, rm; mkfs, mount, untar; post-process ran; template row and directory removed).
- Unit: `RecipeService.build` still issues the same commands (existing tests, through the extracted function).
- Docs: every removed path leaves every document (`tests/unit/test_docs.py`).

### 4.6 Live migration

Deploy the branch, `systemctl stop mshkn`, run the command, start the service, run the full suite. Expected 144 / 6 / 7 with the same seven failures; T0's first bare create pays one template build. Then the journal traceback check.

## 5. PR 7: product changes

Each is pinned in the flow or unit tier; the E2E tier proves it live.

### 5.1 Base-image rule (#73)

```python
def dockerfile_base_image(dockerfile: str) -> str | None:
    """The image of the last FROM (the exported stage), or None if there is no FROM."""
```

Tolerates comments, blank lines, `ARG` before `FROM`, `--platform=…`, `AS name`, any case of `FROM`, and a tag on the image (`mshkn-base:latest` is accepted; the comparison is on the name before the colon). `RecipeService.create` calls it before the content-hash lookup and raises `InvalidInput`:

- no `FROM`: `Dockerfile has no FROM instruction`
- other base: `recipes must be built FROM mshkn-base (the final stage is FROM python:3.12)`

`POST /recipes` therefore returns 422 with that message and no recipe row, build or volume. Last stage rather than first, deviating from the issue's wording, because a multi-stage build that compiles in a public image and copies into `mshkn-base` is exactly what an agent will write, and its exported filesystem is the last stage.

`tests/e2e/test_phase3_capabilities.py::TestBuildFailure` sends `FROM nonexistent-image…`, which the rule now rejects up front; it changes to `FROM mshkn-base\nRUN false`, which fails during the build, which is what the test meant.

### 5.2 Exec timeout

`ExecRequest` gains `timeout_seconds: int = Field(default=60, ge=1, le=600)`. `ComputerService.stream(computer, command, *, timeout: float = 60.0)` passes it to `Guest.stream`, whose protocol already carries `timeout`; `FakeGuest.stream` accepts and records it. The default and the hard-deadline semantics are unchanged; 600 matches the recipe build cap. Values outside the bounds are 422 from validation.

### 5.3 Honest exit after a kill

In `SshGuest._pump`, the final event becomes:

- `exit_status` when the process has one;
- else `128 + signal number` from `process.exit_signal` (137 for SIGKILL, which is what the timeout kill sends, and what an in-guest OOM kill produces);
- else 255.

A timeout kill also logs the command and the deadline. Unit test over the fake SSH process for all three branches.

### 5.4 Recipe image retention

`RecipeService.build` keeps `mshkn-recipe-img-<recipe id>` after a successful export and removes it only on failure. `RecipeService.delete` runs `docker rmi -f` on the tag (not checked; the image may already be gone). With the legacy builder, a rebuild whose Dockerfile shares a prefix with a retained image reuses those layers, which is what T10.8 asserts. Shared layers are shared on disk; #75 gets a note that images now live as long as their recipe and are part of the garbage-collection design.

Installing the buildx plugin (BuildKit, with its own cache and garbage collection) is not part of this PR: it would change the build log format and add a host package for a property the retained image already gives. It is noted in `docs/infrastructure.md` as a future host change if the legacy builder is removed by a Docker upgrade.

## 6. PR 7: the tests

All in `tests/e2e/test_phase10_integration.py` unless noted. Every test destroys its computers, deletes its checkpoints, and (T10.8) deletes its recipes in `finally`. Recipes that other tests share by content hash are built once per host and resolve immediately afterwards.

Helpers in `tests/e2e/conftest.py`:

- `exec_command(client, computer_id, command, timeout=30.0, *, timeout_seconds=None)`: sends `timeout_seconds` when given and sets the HTTP read timeout to `timeout_seconds + 30`.
- `wait_for_recipe(client, recipe_id, timeout=600.0) -> tuple[dict, float]`: polls to a terminal status, returns the recipe body and the elapsed seconds since the call. `create_recipe` uses it and its timeout becomes 600.
- `port_url` moves from `test_phase4_networking.py` into the conftest.

### T10.1: the ffmpeg loop

Recipe: `FROM mshkn-base` plus one `RUN` that installs `ffmpeg` with `--no-install-recommends` and clears the apt lists. Steps: submit, `wait_for_recipe` to `ready` (assert elapsed ≤ 600), create, `ffmpeg -f lavfi -i sine=frequency=440:duration=2 -y /root/tone.wav` with `timeout_seconds=120` (assert exit 0, file size > 0), checkpoint `ffmpeg-base`, fork twice. Fork A: `ffmpeg -i /root/tone.wav -y /root/tone.mp3`. Fork B: `ffmpeg -i /root/tone.wav -t 1 -y /root/short.wav`. Assert A has `tone.mp3` and no `short.wav`, B the reverse, and `ffprobe` reports A's mp3 at about 2 s and B's wav at about 1 s.

### T10.2: apt and pip on a bare computer

Bare computer. `apt-get update && apt-get install -y --no-install-recommends python3 python3-pip python3-venv` with `timeout_seconds=300` (exit 0). `python3 -m venv /root/venv && /root/venv/bin/pip install pandas` with `timeout_seconds=300` (exit 0). Upload a script that writes 200 rows of deterministic data (`group = i % 4`, `value = (i * 7) % 101`) to `/root/data.csv`; the test computes the expected overall mean and per-group sums in Python. Checkpoint `data-ready`. Fork A prints `df.value.mean()`; fork B prints per-group sums. Assert each fork's output matches the expected values and that A's computer has no `sums.txt` while B's has no `mean.txt`.

### T10.3, T10.4

Unchanged.

### T10.5: the scripted agent loop

A small deterministic agent class in the test file with a tool table over the API (create, exec, checkpoint, fork, list checkpoints by label, delete checkpoint, destroy). Task: "name the largest regular file under /etc". Policy: create; explore with `ls /etc | head`; checkpoint `explored`; fork twice; attempt A runs `find /etc -type f -printf '%s %p\n' | sort -rn | head -1`, attempt B runs `find /etc -type f -exec du -b {} + | sort -rn | head -1`; each writes its answer to `/root/answer.txt`; the agent compares the two paths, requires them to agree, checkpoints the attempt with the shorter output under label `answer`, destroys the other attempt and deletes its checkpoints. Assertions: `GET /checkpoints?label=answer` returns exactly one; forking it yields `/root/answer.txt` naming the same file; the losing computer's status is 404; its checkpoints are gone.

### T10.6: a listener survives checkpoint and fork

Uses the phase 4 python recipe (already built on the host). Uploads a 20-line `http.server` script with a global request counter that answers the count, bound to all interfaces on 8080, started with `POST /computers/{computer_id}/exec/bg`. Three GETs over the public URL (`port_url`) return 1, 2, 3. Checkpoint `listening`, fork. A GET on the fork's public URL returns 4; a GET on the original's returns 4 as well, independently. Memory state and the bound socket survived the slot and address change. The script binds `0.0.0.0` on purpose; the test's docstring says a listener bound to the VM's specific address would not.

### T10.7: broken recipe, build log, fix

1. `FROM python:3.12\nRUN true` → 422; the detail names `mshkn-base` and `python:3.12`.
2. `FROM mshkn-base` plus `RUN apt-get update && apt-get install -y --no-install-recommends no-such-package-mshkn-e2e` → `failed` within 600 s; `build_log` contains `no-such-package-mshkn-e2e` and `Unable to locate package`.
3. The same text with `jq` → `ready`; a computer from it runs `jq --version`.
4. The broken text again → a new recipe id (the failed row was replaced), which reaches `failed` again.

### T10.8: incremental toolchain growth

Dockerfile with a per-run nonce as its first layer (`RUN echo run-<uuid>`), then: apt `nodejs npm`; `npm install -g typescript`; `npm install -g esbuild`; `npm install -g prettier`; `mkdir /app && npm init -y`. Cold build time `t_cold` from `wait_for_recipe` (≤ 600). The same text plus `RUN npm install -g nodemon` is a new recipe: `t_incr` ≤ 600 and `t_incr < 0.5 · t_cold`. A computer from the second recipe runs `node --version && tsc --version && esbuild --version && prettier --version && nodemon --version` (exit 0). Print `t_cold`, `t_incr` and the computer's root filesystem usage (`df -m /`) as the size figure; none of the three is asserted beyond the bounds above. Delete the computer, then both recipes (assert 200). The recipe templates directory leaks until #75; the test notes it.

### T7.9: the exec limit belongs to the caller (`tests/e2e/test_phase7_api.py`)

`sleep 65 && echo slept` with `timeout_seconds=90`: exit 0 and `slept` in stdout. `sleep 30` with `timeout_seconds=2`: the stream ends within 10 s of the request, the exit event is 137, and nothing reports success.

### Timing

Estimated additions: T10.1 about 3 min (build dominated), T10.2 about 4 min, T10.5 about 1 min, T10.6 about 1 min, T10.7 about 2 min, T10.8 about 4 min, T7.9 about 1.5 min. Suite from about 14 to about 26 minutes. The suite is 163 tests.

## 7. Documents and issues

Changed in PR 7a: `DEPLOY.md`, `README.md` (layout, running it), `docs/ARCHITECTURE.md` (§5 volume 0, §8 first paragraph, §13 table), `docs/infrastructure.md` (Docker paragraph, buildx note), `docs/plans/README.md` (a PR 7a row).

Changed in PR 7: `docs/plans/2026-03-07-disposable-cloud-computers-test-plan.md` (Phase 10 replaced, T7.9 added; text in the appendix), `README.md` (Exec bullet, Recipes bullet, "what does not exist" list, test counts), `CLAUDE.md` (expected gate 153 / 6 / 4 and the four), `docs/ARCHITECTURE.md` (§6 exec, §8 base rule and image lifetime, §11 timeouts and exit codes), `docs/infrastructure.md` (163 tests), `docs/plans/README.md` (the test plan row, the sentence that historical documents are never edited, the #65 list, a PR 7 row).

Issues:

- #73: decision recorded (enforce, last stage), closed by PR 7.
- #58: decision recorded (after #70, its own PR).
- #65: the four that remain; T10.1, T10.2, T10.5 closed by PR 7.
- #75: images retained per recipe; T10.8 leaves a template directory per run.
- A new issue for PR 7a (one base), referencing this spec, closed by PR 7a.
- #76 closed by the PR that carries this spec and the test plan rewrite.

The T10 rewrite and T7.9 are applied to the test plan in this brainstorm's PR (they are outputs of #76); the count changes elsewhere land with PR 7, when the tests exist.

## 8. Validation

For each PR: the gate locally and in CI (`uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov`, zero warnings, coverage floor held); the live suite, detached, with the summary line and the failing set named in the PR body; the journal traceback count after the run.

- PR 7a: 144 passed, 6 skipped, 7 failed (the seven of #65).
- PR 7: 153 passed, 6 skipped, 4 failed (T8.6, T9.1, T11.2, T11.7 audit).

Any other failure is a regression: fix it or stop and discuss. No xfail, no weakened assertion, no skip to get green.

## 9. Out of scope

- #58, #70, #74, #75 (sequenced above).
- The buildx plugin on the host (§5.4).
- A real-LLM agent test; the first real agent is the next piece of work after #58.
- Recipe layering (`FROM <recipe>`): answered by appending text; revisit only if T10.8's bound stops holding as recipes grow.
- The `capability_cache` table and the vestigial `manifest_*` columns (spec §15 of the quality overhaul: not rebuilt).

## Appendix: the new test plan text

### Phase 7 addition

```
### T7.9 — The Exec Time Limit Belongs to the Caller

- `POST /computers/{id}/exec` takes `timeout_seconds` (default 60, at most 600).
- `sleep 65 && echo slept` with `timeout_seconds: 90` → exit 0, `slept` on stdout.
- `sleep 30` with `timeout_seconds: 2` → the stream ends within 10 s, the exit event is 137 (killed), and nothing reports success.
- **A killed command that reports exit 0 is a lie, and this is where it gets caught.**
```

### Phase 10, replaced

```
## Phase 10: "The Agent Doesn't Care About Your Feelings" (The Generative Loop)

mshkn exists for a generative agent that writes its own recipes and the programs
those computers run. Phases 0 to 9 prove the primitives; this phase proves the
loop: write a recipe, build it, read the failure, fix it, run real tools,
checkpoint, fork, diverge, pick. Every test here is deterministic and runs
against the live host; none needs an LLM.

### T10.1 — The ffmpeg Loop

1. Submit a recipe: `FROM mshkn-base`, one `RUN` that installs ffmpeg.
2. Poll `GET /recipes/{id}` to `ready`. **Within the 600 s build cap.**
3. Create a computer from it; synthesize a two-second tone with ffmpeg.
4. Checkpoint. Fork twice. One fork transcodes to mp3, the other trims to one second.
5. **Each fork has its own output and not the other's; ffprobe durations differ.**

### T10.2 — apt and pip Inside a Bare Computer

1. Create a bare computer (no recipe).
2. `apt-get install python3 python3-pip python3-venv` in the foreground with `timeout_seconds: 300`. **Exit 0.** (VM egress through host NAT, not `docker build`.)
3. `pip install pandas` in a venv, `timeout_seconds: 300`. **Exit 0.**
4. Write a deterministic dataset. Checkpoint.
5. Fork twice: one fork computes the mean, the other per-group sums.
6. **Both match values the test computed itself; neither fork has the other's file.**

### T10.3 — Parallel Exploration

1. Create computer, set up base state, checkpoint.
2. Fork 3 times — try 3 different approaches.
3. Each fork does different work.
4. Agent picks the best fork's checkpoint.
5. Discards the other two.

### T10.4 — Failure Recovery

1. Create computer, do work, checkpoint.
2. Run a command that corrupts state.
3. Fork from the last good checkpoint. Clean state, no corruption.
4. Repeat — this is the core "retry" value prop.

### T10.5 — The Scripted Agent Loop

A deterministic agent with a tool table over the API, no LLM. Task: name the
largest regular file under /etc.

1. Create. Explore. Checkpoint `explored`.
2. Fork two attempts with different commands. Each writes `/root/answer.txt`.
3. Compare: **the answers must agree.** Checkpoint the winner under label `answer`.
4. Destroy the loser; delete its checkpoints.
5. **`GET /checkpoints?label=answer` returns one; forking it yields the answer; the loser is 404 and its checkpoints are gone.**

The old T10.5 gave the tools to the cheapest model and asked whether it could
cope. That claim is no longer part of the definition of done: the first real
agent is built after this phase passes, and it will be measured on its own.

### T10.6 — A Listener Survives Checkpoint and Fork

1. Start an HTTP server with an in-memory request counter, bound to all interfaces.
2. Three requests over the public URL → 1, 2, 3.
3. Checkpoint. Fork.
4. **The fork's public URL answers 4. The original's answers 4 too, independently.**
5. Memory state and the bound socket survived a new slot and a new address.

### T10.7 — Broken Recipe, Build Log, Fix

1. `FROM python:3.12` → **422 naming the rule and the image** (#73). No build.
2. `FROM mshkn-base` installing a package that does not exist → `failed`, **`build_log` names the package and the apt error.**
3. The corrected text → `ready`; a computer from it has the tool.
4. The broken text again → **a new recipe id** (the failed row is replaced), `failed` again.

### T10.8 — Incremental Toolchain Growth

Recipes are content-hashed and do not layer on each other. The agent grows an
image by appending to its own Dockerfile text.

1. A node toolchain recipe with a per-run nonce layer and several global npm installs. Record the cold build time. **Within 600 s.**
2. The same text plus one appended `RUN`. **Rebuild within 600 s and under half the cold time.**
3. A computer from the second recipe has every tool.
4. Delete the computer and both recipes.
```
