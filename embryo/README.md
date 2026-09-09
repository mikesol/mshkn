# embryo

The embryo is the first real agent: a membrane running inside a disposable "brain" computer, reasoning under a policy that starts closed and grows only by proposal and approval. `docs/superpowers/specs/2026-09-08-embryo-design.md` is the design; this directory holds the priors it hatches from — `seed.md`, `policy.json`, `ingress.star`, `Dockerfile.brain`, `liturgy.md` — and `hatch.sh`, the one script a human runs to bring a brain into existence.

## Hatching

```bash
MSHKN_API_URL=https://api.mshkn.dev MSHKN_API_KEY=<account key> \
ANTHROPIC_API_KEY=<key> OPENAI_API_KEY=<key> \
embryo/hatch.sh
```

It mints the brain's scoped key, builds the brain recipe, creates a computer, installs the membrane, writes `/brain/.env` and the priors, checkpoints it as `brain`, destroys the computer and opens the (closed) public ingress door. It prints one JSON line: `{"ingress_url", "rule_id", "key_id", "recipe_id", "checkpoint_id"}`. It needs `uv`, `curl` and `jq` on the machine that runs it.

## Speaking through each door

```bash
# root, through the authenticated door (the account key)
B64=$(printf '%s' 'Hello. I am the one who hatched you. Tell me what you are and what you can do.' | base64 -w0)
curl -fsS -X POST "$MSHKN_API_URL/checkpoints/fork" -H "Authorization: Bearer $MSHKN_API_KEY" \
  -H 'Content-Type: application/json' \
  --data "{\"label\": \"brain\", \"exec\": \"membrane root say $B64\", \"self_destruct\": true, \"exclusive\": \"error_on_conflict\"}" \
  | jq -r .exec_stdout
# root commands: membrane root list | approve <id> | reject <id> <b64> | disable <verb> | revert <id>

# a signed message through the public door (after turn 2 has been approved)
printf '%s' 'Who am I?' > msg && ssh-keygen -Y sign -f ~/.ssh/id_ed25519 -n mshkn msg
PAYLOAD=$(jq -cn --rawfile m msg --arg s "$(base64 -w0 msg.sig)" '{msg: $m, sig: $s}')
curl -fsS -X POST "$INGRESS_URL" -H 'Content-Type: application/json' \
  --data "{\"b64\": \"$(printf '%s' "$PAYLOAD" | base64 -w0)\"}" | jq -r .exec_stdout
```

A 409 from either door means a turn is in progress: retry.

## The brain disk

Everything under `/brain` survives the checkpoint as files, and everything the membrane changes is one of them:

| File | What |
|---|---|
| `/brain/state.json` | The one mutable document: the policy, the self-description, the catalog, the proposals, the trials, the inbox, the turn window, the turn counter, the principals seen and the previously applied policy and prompt. A command loads it once, saves it once at the end, and the save writes a temp file and renames it over the old one, so a command that crashes leaves the previous state byte for byte (#100). |
| `/brain/seed.md`, `/brain/policy.json` | The priors, uploaded by `embryo/hatch.sh` and never written again. `policy.json` is read once, to seed the first `state.json`; after that the live policy is in the state. |
| `/brain/.env` | The scoped key, the model keys and the API URL. |
| `/brain/memory/` | mem0's store. |
