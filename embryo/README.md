# embryo

The embryo is the first real agent: a membrane running inside a disposable "brain" computer, reasoning under a policy that starts closed and grows only by proposal and approval. `docs/superpowers/specs/2026-09-08-embryo-design.md` is the design; this directory holds the priors it hatches from — `seed.md`, `policy.json`, `ingress.star`, `Dockerfile.brain`, `liturgy.md` — and `hatch.sh`, the one script a human runs to bring a brain into existence.

## Hatching

```bash
set -a; . .env; set +a            # MSHKN_API_URL, MSHKN_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY
embryo/hatch.sh
```

It mints the brain's scoped key (with a `relay` section pinning `ANTHROPIC_BASE_URL` as the one target prefix and `membrane resume` on label `brain` as the one wake-up), builds the brain recipe, creates a computer, installs the membrane, writes `/brain/.env` and the priors, checkpoints it as `brain`, destroys the computer and opens the (closed) public ingress door. In `MEMBRANE_MODEL=scripted` mode it first starts a second computer running `membrane serve` and points `ANTHROPIC_BASE_URL` at that computer's route, so the relay calls the scripted model instead of the real one; that computer's id is printed as `server_id`, `null` otherwise. It prints one JSON line: `{"ingress_url", "rule_id", "key_id", "recipe_id", "checkpoint_id", "server_id"}`. It needs `uv`, `curl` and `jq` on the machine that runs it.

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
# root commands: membrane root list | approve <id> | reject <id> <b64> | disable <verb> | revert <id>
# the relay's own command, never run by hand: membrane resume <job_id>

# a signed message through the public door (after turn 2 has been approved)
printf '%s' 'Who am I?' > msg && ssh-keygen -Y sign -f ~/.ssh/id_ed25519 -n mshkn msg
PAYLOAD=$(jq -cn --rawfile m msg --arg s "$(base64 -w0 msg.sig)" '{msg: $m, sig: $s}')
curl -fsS -X POST "$INGRESS_URL" -H 'Content-Type: application/json' \
  --data "{\"b64\": \"$(printf '%s' "$PAYLOAD" | base64 -w0)\"}" | jq -r .exec_stdout
```

A 409 from either door means a turn is in progress: retry. A `say` while a turn is already pending is queued instead and runs next.

## Measuring

The measure (spec §11) is the liturgy spoken to a real model, N times, with the seven postconditions checked afterwards:

```bash
uv run measure --runs 3           # keys and the API from .env; approvals automatic
uv run measure --approve ask      # the pilot reads each proposal and answers approve | reject <reason>
```

`measure` (`embryo/membrane/measure.py`) reads `.env` (the four keys; `BRAIN_API_URL` if the brain dials another address), hatches with `MEMBRANE_MODEL=anthropic`, speaks each turn of `liturgy.md` through its door, approves what is pending, waits for builds, answers a failed build with turn 3 (three times at most), and writes the transcript, every command, the final `list`, the token counts and the verdict to `docs/embryo/<date>-run-<n>/`. It refuses to start if the account already has a `brain`, and tears the brain down at the end unless `--keep`. `--model` picks the model id (`claude-opus-5` by default) and `--effort` the run's default effort, which a turn raises from its own tool list or at the model's request and never lowers (#122). The signing key it speaks with is generated per run, named `mike`, and kept in a temp directory. `docs/embryo/README.md` is the tally.

## The brain disk

Everything under `/brain` survives the checkpoint as files, and everything the membrane changes is one of them:

| File | What |
|---|---|
| `/brain/state.json` | The one mutable document: the policy, the self-description, the catalog, the proposals, the trials, the inbox, the turn window, the turn counter, the principals seen, the previously applied policy and prompt, and (#110) the pending turn and the queue. A command loads it once, saves it once at the end, and the save writes a temp file and renames it over the old one, so a command that crashes leaves the previous state byte for byte (#100). |
| `/brain/seed.md`, `/brain/policy.json` | The priors, uploaded by `embryo/hatch.sh` and never written again. `policy.json` is read once, to seed the first `state.json`; after that the live policy is in the state. |
| `/brain/.env` | The scoped key, the model keys, the API URL and `ANTHROPIC_BASE_URL`. |
| `/brain/memory/` | mem0's store. |
