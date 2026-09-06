"""One implementation of "run a command on a fresh computer, then maybe checkpoint
and destroy it": REST create, REST fork, ingress create/fork, and the deferred
drain all go through here (spec §6.4)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from mshkn.db import claim_deferred_by_label
from mshkn.models import CheckpointTrigger, EphemeralResult, ExecSpec
from mshkn.services.callback import deliver_callback

if TYPE_CHECKING:
    import aiosqlite
    import httpx

    from mshkn.models import Account, Checkpoint, Computer
    from mshkn.runtime import BackgroundTasks
    from mshkn.services.checkpoints import CheckpointService
    from mshkn.services.computers import ComputerService

logger = logging.getLogger(__name__)


class Lifecycle:
    def __init__(
        self,
        db: aiosqlite.Connection,
        computers: ComputerService,
        checkpoints: CheckpointService,
        tasks: BackgroundTasks,
        http: httpx.AsyncClient,
    ) -> None:
        self.db = db
        self.computers = computers
        self.checkpoints = checkpoints
        self.tasks = tasks
        self.http = http

    async def run_ephemeral(
        self,
        account: Account,
        computer: Computer,
        spec: ExecSpec,
        *,
        source_checkpoint: Checkpoint | None,
    ) -> EphemeralResult:
        if spec.command is None:
            return EphemeralResult(computer.id, None, None, None, None)
        result = await self.computers.exec(computer, spec.command)
        created_checkpoint_id: str | None = None
        if spec.self_destruct:
            label = source_checkpoint.label if source_checkpoint is not None else spec.label
            ckpt = await self.checkpoints.create(
                computer, label=label, trigger=CheckpointTrigger.SELF_DESTRUCT
            )
            created_checkpoint_id = ckpt.id
            await self.computers.destroy(computer.id)
            if spec.callback_url:
                payload = {
                    "computer_id": computer.id,
                    "checkpoint_id": source_checkpoint.id if source_checkpoint else None,
                    "label": label,
                    "exec_exit_code": result.exit_code,
                    "exec_stdout": result.stdout,
                    "exec_stderr": result.stderr,
                    "created_checkpoint_id": created_checkpoint_id,
                }
                self.tasks.spawn(
                    deliver_callback(self.http, spec.callback_url, payload),
                    name=f"callback:{computer.id}",
                )
            logger.info(
                "Self-destruct: computer %s checkpointed as %s and destroyed",
                computer.id,
                created_checkpoint_id,
            )
            if label:
                self.spawn_drain(account, label)
        return EphemeralResult(
            computer_id=computer.id,
            exec_exit_code=result.exit_code,
            exec_stdout=result.stdout,
            exec_stderr=result.stderr,
            created_checkpoint_id=created_checkpoint_id,
        )

    def spawn_drain(self, account: Account, label: str) -> None:
        self.tasks.spawn(self.drain_deferred(account, label), name=f"deferred:{label}")

    async def drain_after_destroy(self, account: Account, computer: Computer) -> None:
        if not computer.source_checkpoint_id:
            return
        source = await self.checkpoints.get_owned(account, computer.source_checkpoint_id)
        if source.label:
            self.spawn_drain(account, source.label)

    async def drain_deferred(self, account: Account, label: str) -> None:
        """Process every queued fork for a label on one new computer.

        The claim is a single DELETE … RETURNING, so a destroy and an idle reap
        draining the same label at once cannot both fork. Each request's exec
        is written to /tmp/exec/N.txt; the command run is the last meta_exec if
        any, else the execs joined by newlines; self_destruct if any asked;
        callback_url is the last one given.
        """
        items = await claim_deferred_by_label(self.db, label)
        if not items:
            return
        try:
            latest = await self.checkpoints.latest_for_label(account, label)
            if latest is None:
                logger.warning("No checkpoints found with label %s for deferred processing", label)
                return
            payloads = [json.loads(d.request_payload) for d in items]
            recipe_id = next(
                (p["recipe_id"] for p in reversed(payloads) if p.get("recipe_id")), None
            )
            computer = await self.computers.fork(
                account, latest, recipe_id=recipe_id or latest.recipe_id
            )
            execs = [p.get("exec") or "" for p in payloads]
            writes = ["mkdir -p /tmp/exec"]
            for i, cmd in enumerate(execs):
                escaped = cmd.replace("'", "'\\''")
                writes.append(f"printf '%s' '{escaped}' > /tmp/exec/{i}.txt")
            await self.computers.exec(computer, " && ".join(writes))
            meta_exec = next(
                (p["meta_exec"] for p in reversed(payloads) if p.get("meta_exec")), None
            )
            command = meta_exec or "\n".join(c for c in execs if c)
            if not command:
                logger.info(
                    "Deferred batch for %s had no command; computer %s left running",
                    label,
                    computer.id,
                )
                return
            spec = ExecSpec(
                command=command,
                self_destruct=any(p.get("self_destruct") for p in payloads),
                callback_url=next(
                    (p["callback_url"] for p in reversed(payloads) if p.get("callback_url")), None
                ),
                label=label,
                meta_exec=meta_exec,
            )
            outcome = await self.run_ephemeral(account, computer, spec, source_checkpoint=latest)
            logger.info(
                "Processed %d deferred request(s) for label %s -> computer %s (exit=%s)",
                len(items),
                label,
                computer.id,
                outcome.exec_exit_code,
            )
        except Exception:
            logger.exception("Failed to process deferred queue for label %s", label)
