# Promoted: web-search

Run `web-search/2026-09-17-run-6`, membrane `f8f72a76ef76cf1170df5aff7111af116cd36b40`, promoted 2026-09-17T16:11:05+00:00, promoted from a run with 0 re-asks.
Dependents start from these labels; `capability promote` overwrites this file.

| Working label | Promoted label | Checkpoint |
|---|---|---|
| `brain` | `capability/web-search/brain` | `ckpt-176e7170862e` |
| `verb/call_counter` | `capability/web-search/verb/call_counter` | `ckpt-6300f5360704` |
| `verb/tavily_extract` | `capability/web-search/verb/tavily_extract` | `ckpt-a400af297409` |
| `verb/tavily_search` | `capability/web-search/verb/tavily_search` | `ckpt-2f4a54c3cfe9` |

```json
{
 "base_url": "https://ai-gateway.vercel.sh",
 "body_extra": "{\"providerOptions\":{\"gateway\":{\"only\":[\"openai\"]}}}",
 "brain_recipe": "rcp-6e586c3878f2",
 "capability": "web-search",
 "default_effort": "off",
 "key_dir": "/home/mike/.mshkn/keys/hatch/2026-09-16-run-10",
 "key_id": "key-5d3f734ba5b6",
 "labels": {
  "brain": "ckpt-176e7170862e",
  "verb/call_counter": "ckpt-6300f5360704",
  "verb/tavily_extract": "ckpt-a400af297409",
  "verb/tavily_search": "ckpt-2f4a54c3cfe9"
 },
 "membrane": {
  "commit": "f8f72a76ef76cf1170df5aff7111af116cd36b40",
  "dirty": false
 },
 "model": "openai/gpt-5.6-sol",
 "promoted_at": "2026-09-17T16:11:05+00:00",
 "pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILYgW0TAsroVv6uuC2N5pOkHqmmUB4u3VdDk3OXRVLnX mike",
 "reasks": 0,
 "recipe_ids": [
  "rcp-150fca486978",
  "rcp-15ef0e5a208c",
  "rcp-2512963336dd",
  "rcp-4341c4662210",
  "rcp-5765fd4ccf05",
  "rcp-6a3c3d620b87",
  "rcp-6e586c3878f2"
 ],
 "rotated_from": null,
 "rule_id": "ir_t3cTQGIw-tHvgh4hAWqzdNJP1SU",
 "run": "web-search/2026-09-17-run-6",
 "started_from": "hatch/2026-09-16-run-10"
}
```
