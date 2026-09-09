"""The console script. `membrane say <b64>` is the public door; `membrane root
<command>` the authenticated one; `membrane resume <job_id>` the relay's
wake-up; `membrane serve` the scripted model's server. One command per process;
state is loaded from /brain and saved before exit. Every command but resume
settles a pending turn first; what that prints goes to stderr, so a command's
stdout is its own."""

from __future__ import annotations

import asyncio
import sys
import time
from typing import TYPE_CHECKING

from membrane.commands import USAGE as USAGE  # re-exported: membrane.cli's public USAGE constant
from membrane.commands import root
from membrane.config import load_settings
from membrane.state import Brain
from membrane.turn import TURN_DEADLINE, Context, resume, say, settle

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from membrane.memory import MemoryStore
    from membrane.mshkn import MshknApi

ARITY = {"say": 1, "list": 0, "approve": 1, "reject": 2, "disable": 1, "revert": 1}


def _valid(argv: list[str]) -> bool:
    if len(argv) == 2 and argv[0] in ("say", "resume"):
        return True
    if len(argv) >= 2 and argv[0] == "root" and argv[1] in ARITY:
        return len(argv) == 2 + ARITY[argv[1]]
    return False


def _stderr(text: str) -> None:
    print(text, end="", file=sys.stderr)


async def run(
    argv: list[str],
    *,
    brain_dir: Path | None = None,
    api: MshknApi | None = None,
    memory: MemoryStore | None = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    err: Callable[[str], None] = _stderr,
) -> tuple[str, int]:
    if not _valid(argv):
        return USAGE, 2
    settings = load_settings(brain_dir)
    brain = Brain(settings.brain)
    state = brain.state()
    owned_api = api is None
    if api is None:
        from membrane.mshkn import Mshkn

        api = Mshkn.connect(settings)

    def open_memory() -> MemoryStore:
        from membrane.memory import Mem0Store

        return Mem0Store.open(settings, brain.memory_dir)

    ctx = Context(
        brain=brain,
        state=state,
        api=api,
        settings=settings,
        deadline=now() + TURN_DEADLINE,
        memory=memory,
        open_memory=open_memory,
        now=now,
        sleep=sleep,
    )
    try:
        if argv[0] != "resume":
            notes = await settle(ctx)
            if notes:
                err(notes)
        if argv[0] == "say":
            out, code = await say(ctx, payload_b64=argv[1], door="ingress"), 0
        elif argv[0] == "resume":
            out, code = await resume(ctx, argv[1]), 0
        else:
            out, code = await root(argv[1:], ctx)
        # P14: state is durable only when the command actually succeeded; a
        # crashed turn leaves state.json exactly as it was, and its traceback
        # goes to mshkn's exec log instead. Never move this into `finally`.
        brain.save(state)
        return out, code
    finally:
        if memory is None and ctx.memory is not None:
            ctx.memory.close()
        if owned_api:
            from membrane.mshkn import Mshkn

            if isinstance(api, Mshkn):
                await api.aclose()


def main() -> None:
    argv = sys.argv[1:]
    if argv[:1] == ["serve"]:
        from membrane.serve import main as serve_main

        serve_main(argv[1:])
        return
    out, code = asyncio.run(run(argv))
    print(out, end="")
    sys.exit(code)
