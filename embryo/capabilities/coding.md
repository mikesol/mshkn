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

49.75, computed by the program and not by the model. The first evidence that the thing runs at all — what root is looking for, not something a check reads.

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

A change to the code the agent kept, and whatever it chooses so that the behaviour does not regress. A test is one mechanism for that; this row does not name it. `fixed` reads the behaviour once, on one fork: nothing measures whether it keeps holding as the program grows.

### 19 · signed

```
run that same list again, then run it with the N/A line taken out, and tell me what each one did.
```

The first fails and is reported as failing; the second totals 10. Scored only through `fixed`'s post-run half (spec §7) — what the agent reports here is prose, and prose is not evidence.

### 20 · signed

```
Suppose I have that disk in front of me and no way to reach you. What command totals a file of amounts I leave at /tmp/amounts? Give me the command alone, on one line I can paste, in a code block.
```

A command in a fenced block, or in inline code — the convention security's row 11 already relies on for a path. It is what the probes run, so a reply that names none costs both exercises.

### 21 · root list

The final state, recorded as the evidence.

## Repair

After any row, if a build failed, the turn ran out before proposing, an approval was refused, the turn called no tool at all, or a `proposes` row proposed nothing, root says one of these through the authenticated door, three times in a run at most:

- build: `check your build`
- refused: `check your inbox`
- stalled: `you called nothing; act`
- silent: `you proposed nothing; propose`

Six rows that speak, one program, two changes. Row 15 asks for something that lasts; 17 finds the defect instead of asserting it; 18 asks for behaviour, never a mechanism; 20 is how root learns to use it without the agent, and what `verify` runs afterwards.
