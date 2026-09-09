"""The console script. `membrane say <b64>` is the public door; `membrane root
<command>` is the authenticated one (plan decision 3). One command per process;
state is loaded from /brain and saved before exit."""

from __future__ import annotations

import asyncio
import sys
import time
from typing import TYPE_CHECKING

from membrane.commands import USAGE as USAGE  # re-exported: membrane.cli's public USAGE constant
from membrane.commands import root
from membrane.config import load_settings
from membrane.state import Brain
from membrane.turn import TURN_DEADLINE, say

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from membrane.memory import MemoryStore
    from membrane.model import Model
    from membrane.mshkn import MshknApi

ARITY = {"say": 1, "list": 0, "approve": 1, "reject": 2, "disable": 1, "revert": 1}


def _valid(argv: list[str]) -> bool:
    if len(argv) == 2 and argv[0] == "say":
        return True
    if len(argv) >= 2 and argv[0] == "root" and argv[1] in ARITY:
        return len(argv) == 2 + ARITY[argv[1]]
    return False


async def run(
    argv: list[str],
    *,
    brain_dir: Path | None = None,
    api: MshknApi | None = None,
    model: Model | None = None,
    memory: MemoryStore | None = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[str, int]:
    if not _valid(argv):
        return USAGE, 2
    settings = load_settings(brain_dir)
    brain = Brain(settings.brain)
    state = brain.state()
    deadline = now() + TURN_DEADLINE
    needs_model = argv[0] == "say" or argv[1] == "say"
    owned_api = api is None
    if api is None:
        from membrane.mshkn import Mshkn

        api = Mshkn.connect(settings)
    owned_memory = memory is None and needs_model
    if needs_model:
        if model is None:
            from membrane.model import build_model

            model = build_model(settings)
        if memory is None:
            from membrane.memory import Mem0Store

            memory = Mem0Store.open(settings, brain.memory_dir)
    try:
        if argv[0] == "say":
            assert model is not None and memory is not None
            out = await say(
                brain=brain,
                state=state,
                api=api,
                model=model,
                memory=memory,
                payload_b64=argv[1],
                door="ingress",
                deadline=deadline,
                now=now,
                sleep=sleep,
            )
            code = 0
        else:
            out, code = await root(
                argv[1:],
                brain=brain,
                state=state,
                api=api,
                model=model,
                memory=memory,
                deadline=deadline,
                now=now,
                sleep=sleep,
            )
        # P14: state is durable only when the command actually succeeded; a
        # crashed turn leaves state.json exactly as it was, and its traceback
        # goes to mshkn's exec log instead. Never move this into `finally`.
        brain.save(state)
        return out, code
    finally:
        if owned_memory and memory is not None:
            memory.close()
        if owned_api:
            from membrane.mshkn import Mshkn

            if isinstance(api, Mshkn):
                await api.aclose()


def main() -> None:
    out, code = asyncio.run(run(sys.argv[1:]))
    print(out, end="")
    sys.exit(code)
