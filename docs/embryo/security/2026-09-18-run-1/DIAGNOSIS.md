# Why 2026-09-18-run-1 got a 401

The verb sent a mangled token. The apparatus was fine.

## The line

`read_bearer_page`'s entrypoint ends with, as written into `/verb/run.sh`:

```sh
printf "oauth2-bearer = \\"%s\\"\\n" "$token" | curl --config - ...
```

One backslash level too many. The agent escaped for the JSON and the Dockerfile
`printf '%s\n' '…'` list, and the surviving `\\` then had to survive a *shell*
double-quoted string too. It does not: in `"…\\"…"` the `\\` collapses to one
backslash and the following `"` closes the string.

## What it emits

```
$ printf "oauth2-bearer = \\"%s\\"\\n" "$token" | od -c
o a u t h 2 - b e a r e r   =
\ Q g n I X P V c B m D k y E Y _ e A 5 0 H u _ f R m S v g j a
M X G - O 6 w 6 k a j Y \ n
```

No quotes around the value, a literal backslash in front of the token, and a
literal `\n` after it. curl reads that as an unquoted value and sends:

```
Authorization: Bearer \\QgnIXPVcBmDkyEY_eA50Hu_fRmSvgjaMXG-O6w6kajY\\n
```

`membrane/page.py` compares the header against `"Bearer " + token` exactly, so:
401. The verb's own guards passed first, because the *file* was perfect — the
corruption happens after the token is read.

The intended line, `printf "oauth2-bearer = \"%s\"\n"`, sends the right header;
so does the explicit `header = "Authorization: Bearer …"` form that run-3 used.

## What was ruled out, and how

Each of these was measured against the live account, with no model in the loop.

- **curl's `--oauth2-bearer` does send the header.** Guest curl is 8.5.0, the same
  build as this box. Six generated `token_urlsafe(32)` values through
  `curl --config -` against an echo server: header correct every time.
- **The apparatus serves and gates correctly.** `membrane.page.serve` stood the
  page up; from a second computer, `oauth2-bearer` and an explicit `Authorization`
  header both returned the body with HTTP 200, and no auth returned 401.
- **The secret transports intact.** Uploaded 43 bytes; sha256 of the token inside
  the guest matched the host's.
- **The secret survives `provision`'s checkpoint.** `/run` is tmpfs, so this was
  the live suspicion. Upload to `/run/secrets/`, checkpoint under the chain,
  destroy, fork the checkpoint: the file is present, readable, 43 bytes, same sha.

## Note

Two of the four security runs failed on the agent's own shell quoting, in
unrelated ways: run-2's `case "$token" in *"$(printf "\n")"*)` matches every
string because command substitution strips trailing newlines, and this one. See
the issue filed alongside this.
