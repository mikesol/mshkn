# Promoted: security

Run `security/2026-09-18-run-2`, membrane `f8f72a76ef76cf1170df5aff7111af116cd36b40`, promoted 2026-09-18T09:22:58+00:00, promoted from a run with 0 re-asks.
Dependents start from these labels; `capability promote` overwrites this file.

| Working label | Promoted label | Checkpoint |
|---|---|---|
| `brain` | `capability/security/brain` | `ckpt-c21dac5e5cd9` |
| `verb/call_counter` | `capability/security/verb/call_counter` | `ckpt-c68811135216` |
| `verb/read_token_page` | `capability/security/verb/read_token_page` | `ckpt-32d072a9fc48` |
| `verb/read_token_page_second` | `capability/security/verb/read_token_page_second` | `ckpt-26be8df54518` |

```json
{
 "base_url": "https://ai-gateway.vercel.sh",
 "body_extra": "{\"providerOptions\":{\"gateway\":{\"only\":[\"openai\"]}}}",
 "brain_recipe": "rcp-6e586c3878f2",
 "capability": "security",
 "default_effort": "off",
 "key_dir": "/home/mike/.mshkn/keys/hatch/2026-09-16-run-10",
 "key_id": "key-5d3f734ba5b6",
 "labels": {
  "brain": "ckpt-c21dac5e5cd9",
  "verb/call_counter": "ckpt-c68811135216",
  "verb/read_token_page": "ckpt-32d072a9fc48",
  "verb/read_token_page_second": "ckpt-26be8df54518"
 },
 "membrane": {
  "commit": "f8f72a76ef76cf1170df5aff7111af116cd36b40",
  "dirty": false
 },
 "model": "openai/gpt-5.6-sol",
 "promoted_at": "2026-09-18T09:22:58+00:00",
 "pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILYgW0TAsroVv6uuC2N5pOkHqmmUB4u3VdDk3OXRVLnX mike",
 "reasks": 0,
 "recipe_ids": [
  "rcp-1e84988e416e",
  "rcp-2512963336dd",
  "rcp-4341c4662210",
  "rcp-50db50858b68",
  "rcp-6a3c3d620b87",
  "rcp-6e586c3878f2"
 ],
 "rotated_from": null,
 "rule_id": "ir_t3cTQGIw-tHvgh4hAWqzdNJP1SU",
 "run": "security/2026-09-18-run-2",
 "started_from": "hatch/2026-09-16-run-10"
}
```
