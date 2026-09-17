# embryo

The embryo is the first real agent: a membrane running inside a disposable "brain" computer, reasoning under a policy that starts closed and grows only by proposal and approval. `docs/superpowers/specs/2026-09-08-embryo-design.md` is the design; this directory holds the priors it hatches from — `seed.md`, `policy.json`, `ingress.star`, `Dockerfile.brain` — and `hatch.sh`, the one script a human runs to bring a brain into existence; the capabilities it grows by are under `embryo/capabilities/`.

## Hatching

```bash
set -a; . .env; set +a            # MSHKN_API_URL, MSHKN_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY
embryo/hatch.sh
```

It mints the brain's scoped key (with a `relay` section pinning `ANTHROPIC_BASE_URL` as the one target prefix and `membrane resume` on label `brain` as the one wake-up), builds the brain recipe, creates a computer, installs the membrane, writes `/brain/.env` and the priors, checkpoints it as `brain`, destroys the computer and opens the (closed) public ingress door. In `MEMBRANE_MODEL=scripted` mode it first starts a second computer running `membrane serve` and points `ANTHROPIC_BASE_URL` at that computer's route, so the relay calls the scripted model instead of the real one; that computer's id is printed as `server_id`, `null` otherwise. It prints one JSON line: `{"ingress_url", "rule_id", "key_id", "recipe_id", "checkpoint_id", "server_id"}`. It needs `uv`, `curl` and `jq` on the machine that runs it.

**The model keys are purpose-built** (spec §7.1 of `docs/superpowers/specs/2026-09-12-capabilities-design.md`): `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` in the operator's environment when `hatch.sh` runs are keys minted for this brain, with their own spend limit, revocable in one act without touching any other key (an Anthropic workspace per brain, an OpenAI project key per brain, or a per-brain key from the model gateway of #127). Never the operator's account keys. A leaked checkpoint then confers that brain's budget until its key is revoked, and nothing more. mshkn never holds a third-party credential: the relay forwards the headers the brain gave it and forgets them on settle.

## Speaking through each door

A `say` no longer runs the model inline: it posts the request to the host's relay, saves the pending turn, and answers at once with `{"turn": N, "job": "rj-…"}`. The relay calls the model and forks `brain` with `membrane resume <job_id>` when it settles — the only command the relay itself may run — and the reply, once the fork closes the turn, is read back from `membrane root list`'s `window`.

```bash
# root, through the authenticated door (the account key)
B64=$(printf '%s' 'Hello. I am the one who hatched you. Tell me what you are and what you can do.' | base64 -w0)
curl -fsS -X POST "$MSHKN_API_URL/checkpoints/fork" -H "Authorization: Bearer $MSHKN_API_KEY" \
  -H 'Content-Type: application/json' \
  --data "{\"label\": \"brain\", \"exec\": \"membrane root say $B64\", \"self_destruct\": true, \"exclusive\": \"error_on_conflict\"}" \
  | jq -r .exec_stdout                          # {"turn": N, "job": "rj-…"}

# read the reply once the relay has woken the brain (repeat until .window has the turn)
curl -fsS -X POST "$MSHKN_API_URL/checkpoints/fork" -H "Authorization: Bearer $MSHKN_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"label": "brain", "exec": "membrane root list", "self_destruct": true, "exclusive": "error_on_conflict"}' \
  | jq -r .exec_stdout | jq '.window[-1]'
# root commands: membrane root list | approve <id> | reject <id> <b64> | disable <verb> | revert <id> | provide <verb> <name>
# the relay's own command, never run by hand: membrane resume <job_id>

# a signed message through the public door (after turn 2 has been approved)
printf '%s' 'Who am I?' > msg && ssh-keygen -Y sign -f ~/.ssh/id_ed25519 -n mshkn msg
PAYLOAD=$(jq -cn --rawfile m msg --arg s "$(base64 -w0 msg.sig)" '{msg: $m, sig: $s}')
curl -fsS -X POST "$INGRESS_URL" -H 'Content-Type: application/json' \
  --data "{\"b64\": \"$(printf '%s' "$PAYLOAD" | base64 -w0)\"}" | jq -r .exec_stdout
```

A 409 from either door means a turn is in progress: retry. A `say` while a turn is already pending is queued instead and runs next.

## Running a capability

A capability (`docs/superpowers/specs/2026-09-12-capabilities-design.md`) is a markdown script under `embryo/capabilities/`: rows of words through a door, and the postconditions that judge the result. `hatch.md` is the first. A capability may also have a Python module beside it, `<name>.py`, for the apparatus its run needs — a server to start, a value to put in the context, a check only it needs — and the words stay in the markdown.

```bash
uv run capability run hatch --runs 3           # keys and the API from .env; approvals automatic
uv run capability run hatch --approve ask      # the pilot reads each proposal and answers approve | reject <reason>
uv run capability run hatch --keep             # leave the brain on the account, so it can be promoted
uv run capability promote hatch docs/embryo/hatch/<date>-run-<n>
```

`capability run` (`embryo/membrane/capability.py`) reads `.env` (the four keys; `BRAIN_API_URL` if the brain dials another address), hatches with `MEMBRANE_MODEL=anthropic` (or, for a capability with dependencies, forks the last dependency's promoted checkpoints into the working labels), speaks each row through its door, approves what is pending, waits for builds, answers a failed build or a refused approval with the capability's repair phrase (three times in a run at most), asks again — same words, same door, labelled `<label>-again-<n>` and counted in `run.json`'s `reasks` — every public row since the last policy change that called no verb, proposed no verb, and whose principal the change just gained a ready verb that is not a door hook (twice per row at most, #170), judges the named postconditions, and writes the transcript, every command, the final `list`, the token counts and the verdict to `docs/embryo/<name>/<date>-run-<n>/`. It refuses to start if the account already has a `brain`, and tears the brain down at the end unless `--keep`. `--model` picks the model id (`claude-opus-5` by default) and `--effort` the run's default effort, which a turn raises from its own tool list or at the model's request and never lowers (#122). The signing key it speaks with is named `mike` and lives on the operator's machine, under `~/.mshkn/keys/<capability>/<run>` unless `--key-dir` says otherwise, never under `docs/`; a capability that hatches generates one and `run.json` records the directory and the public key line, and a dependent signs with its lineage's key, because the promoted identity hook trusts the key that hatch's row 2 handed the agent.

A capability whose rows ask for something root holds prepares it in its module (`embryo/capabilities/security.py` starts a token-gated page and yields `url` and `token` into the run's context). After any row settles, a ready verb whose `requires` names something root has not provided is provisioned by the driver from the path the reply named (a fenced block holding the path alone, or inline code): with the account key and outside every door, a computer is created from the verb's chain head (or its recipe when the chain is empty), the token is uploaded to that path, the computer is checkpointed under the chain and destroyed, and root says `provide`. The four commands are recorded by name in `commands/`, never with the value, and `nothing_by_hand` admits exactly one such sequence per `provide`. A reply that names no path earns the capability's `provide` repair phrase. With `--approve ask` the pilot reads each placement and can override the path or skip it.

A capability's module may also define `verify(doors, turns, final, log)`, called once after the final listing is recorded and before the checks are judged, and handed the turns and that listing: a capability whose evidence is what root can still do afterwards probes it there, with the account key and through no door, and its findings reach its own checks through module state. Two rules follow from where it sits. `doors.sent` is snapshotted after it returns, so a probe that reaches for `doors.root` or `doors.provision` is charged to the agent by `nothing_by_hand`. And what escapes it is swallowed into the driver's log, which is not the committed record, so a probe that can fail must catch its own failure and write it into that state — otherwise a host hiccup reaches the checks as the agent having built nothing. `embryo/capabilities/coding.py` is the worked example.

`capability promote` takes a kept, passing run, copies its `brain` and every `verb/<name>` head under `capability/<name>/`, writes `docs/embryo/<name>/PROMOTED.md`, and drops the working labels. The run's key, ingress rule and recipes stay: they are the lineage a dependent reuses. `docs/embryo/README.md` is the index.

## Rotation

After a capability has been grown and promoted on a lineage, root rotates the lineage's model keys and cancels the hatch-time ones (spec §7.1). Between runs, never inside one, so the invariant that judges a run's commands never sees it:

1. Mint fresh purpose-built keys.
2. Place them on the promoted brain by the sequence the driver uses for a verb's secret, on the brain's chain: create a computer from the head of `capability/<name>/brain`, upload the new `/brain/.env` (the same names `hatch.sh` writes; `chmod 600` it), checkpoint under the same label, destroy.
3. Record it in `docs/embryo/<name>/PROMOTED.md`: set `rotated_from` in the JSON block to the previous record's run and brain checkpoint id (`"hatch/2026-09-13-run-5 ckpt-…"`), and the `brain` row's checkpoint to the new head. The record still loads (`Promotion.rotated_from`).
4. Cancel the hatch-time keys. Every checkpoint that ever held them, the measure rounds' included, is worthless for spend.

A dependent forked from the old promotion dies on its first model call once step 4 runs, which is the point. `capability rotate <name>` can make steps 2 and 3 mechanical; the first rotation is four API calls and an edit.

## The brain disk

Everything under `/brain` survives the checkpoint as files, and everything the membrane changes is one of them:

| File | What |
|---|---|
| `/brain/state.json` | The one mutable document: the policy, the self-description, the catalog, the proposals, the trials, the inbox, the turn window, the turn counter, the principals seen, the previously applied policy and prompt, and (#110) the pending turn and the queue. A command loads it once, saves it once at the end, and the save writes a temp file and renames it over the old one, so a command that crashes leaves the previous state byte for byte (#100). |
| `/brain/seed.md`, `/brain/policy.json` | The priors, uploaded by `embryo/hatch.sh` and never written again. `policy.json` is read once, to seed the first `state.json`; after that the live policy is in the state. |
| `/brain/.env` | The scoped key, the model keys, the API URL and `ANTHROPIC_BASE_URL`. |
| `/brain/memory/` | mem0's store. |
