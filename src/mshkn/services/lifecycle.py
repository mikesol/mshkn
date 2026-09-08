"""One implementation of "run a command on a fresh computer, then maybe checkpoint
and destroy it": REST create, REST fork, ingress create/fork, and the deferred
drain all go through here (spec §6.4). Every run with a command leaves an
exec_log row, the caller's record of a turn once the computer is gone (#58)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from mshkn.db import (
    claim_deferred_by_label,
    delete_exec_logs_before,
    get_exec_log,
    insert_exec_log,
    set_exec_log_checkpoint,
)
from mshkn.errors import NotFound
from mshkn.models import CheckpointTrigger, EphemeralResult, ExecLog, ExecSpec
from mshkn.services.callback import deliver_callback

if TYPE_CHECKING:
    import aiosqlite
    import httpx

    from mshkn.host import ExecResult
    from mshkn.models import Account, Checkpoint, Computer
    from mshkn.runtime import BackgroundTasks
    from mshkn.services.checkpoints import CheckpointService
    from mshkn.services.computers import ComputerService

logger = logging.getLogger(__name__)

# Stored stdout and stderr are each bounded to this many bytes; the response and
# the callback still carry the whole output.
EXEC_LOG_OUTPUT_BYTES = 8192


def truncate_output(text: str, limit: int = EXEC_LOG_OUTPUT_BYTES) -> tuple[str, bool]:
    """Bound text to about `limit` bytes, keeping the head and the tail.

    The tail is where a traceback ends and the head is where a run says what it
    set out to do, so both survive; a marker in between says how much went.
    The cut is on bytes and a character split by it is dropped, never mangled.
    """
    data = text.encode()
    if len(data) <= limit:
        return text, False
    half = limit // 2
    head = data[:half].decode(errors="ignore")
    tail = data[-half:].decode(errors="ignore")
    dropped = len(data) - len(head.encode()) - len(tail.encode())
    return f"{head}\n[mshkn: {dropped} bytes truncated]\n{tail}", True


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
        label = source_checkpoint.label if source_checkpoint is not None else spec.label
        # Recorded before the self-destruct, so a checkpoint that fails to be
        # taken does not also lose the output of the run that led to it.
        await self._record(computer, spec.command, result, source_checkpoint, label)
        created_checkpoint_id: str | None = None
        if spec.self_destruct:
            ckpt = await self.checkpoints.create(
                computer, label=label, trigger=CheckpointTrigger.SELF_DESTRUCT
            )
            created_checkpoint_id = ckpt.id
            await set_exec_log_checkpoint(self.db, computer.id, ckpt.id)
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

    async def _record(
        self,
        computer: Computer,
        command: str,
        result: ExecResult,
        source_checkpoint: Checkpoint | None,
        label: str | None,
    ) -> None:
        stdout, stdout_truncated = truncate_output(result.stdout)
        stderr, stderr_truncated = truncate_output(result.stderr)
        await insert_exec_log(
            self.db,
            ExecLog(
                computer_id=computer.id,
                account_id=computer.account_id,
                source_checkpoint_id=source_checkpoint.id if source_checkpoint else None,
                created_checkpoint_id=None,
                label=label,
                command=command,
                exit_code=result.exit_code,
                stdout=stdout,
                stderr=stderr,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                created_at=datetime.now(UTC).isoformat(),
            ),
        )

    async def exec_log(self, account: Account, computer_id: str) -> ExecLog:
        """The record of the ephemeral run on a computer, alive or gone."""
        log = await get_exec_log(self.db, computer_id)
        if log is None or log.account_id != account.id:
            raise NotFound("No exec log for that computer")
        return log

    async def expire_exec_logs(self) -> int:
        """Delete exec logs older than the configured retention; 0 keeps them all."""
        retention = self.computers.config.exec_log_retention_seconds
        if retention <= 0:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(seconds=retention)).isoformat()
        return await delete_exec_logs_before(self.db, cutoff)

    def spawn_drain(self, account: Account, label: str) -> None:
        self.tasks.spawn(self.drain_deferred(account, label), name=f"deferred:{label}")

    async def drain_after_destroy(self, account: Account, computer: Computer) -> None:
        """Drain the source checkpoint's label, if the computer came from a labelled one."""
        label = await self.checkpoints.source_label(account.id, computer)
        if label:
            self.spawn_drain(account, label)

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
