"""Row builders shared by the unit tier. Every field has a sensible default;
override only what the test is about."""

from __future__ import annotations

from mshkn.models import Account, Checkpoint, Computer, ComputerStatus, Recipe, RecipeStatus


def account_row(id: str = "acct-1", *, api_key: str = "test-key", vm_limit: int = 10) -> Account:  # noqa: A002
    return Account(id=id, api_key=api_key, vm_limit=vm_limit, created_at="2026-03-08T00:00:00")


def computer_row(
    n: int = 1,
    *,
    id: str | None = None,  # noqa: A002
    account_id: str = "acct-1",
    status: ComputerStatus = ComputerStatus.RUNNING,
    source_checkpoint_id: str | None = None,
    recipe_id: str | None = None,
    last_exec_at: str | None = None,
) -> Computer:
    return Computer(
        id=id if id is not None else f"comp-{n}",
        account_id=account_id,
        thin_volume_id=100 + n,
        tap_device=f"tap{n}",
        vm_ip=f"172.16.{n}.2",
        socket_path=f"/tmp/fc-mshkn-comp-{n}.socket",
        firecracker_pid=1000 + n,
        status=status,
        created_at="2026-03-08T00:00:00",
        last_exec_at=last_exec_at,
        source_checkpoint_id=source_checkpoint_id,
        recipe_id=recipe_id,
    )


def checkpoint_row(
    id: str = "ckpt-1",  # noqa: A002
    *,
    account_id: str = "acct-1",
    computer_id: str | None = "comp-1",
    parent_id: str | None = None,
    thin_volume_id: int | None = 50,
    label: str | None = None,
    pinned: bool = False,
    created_at: str = "2026-03-08T00:00:00",
    recipe_id: str | None = None,
) -> Checkpoint:
    return Checkpoint(
        id=id,
        account_id=account_id,
        parent_id=parent_id,
        computer_id=computer_id,
        thin_volume_id=thin_volume_id,
        r2_prefix=f"{account_id}/{id}",
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label=label,
        pinned=pinned,
        created_at=created_at,
        recipe_id=recipe_id,
    )


def recipe_row(
    id: str = "rcp-1",  # noqa: A002
    *,
    account_id: str = "acct-1",
    status: RecipeStatus = RecipeStatus.READY,
    base_volume_id: int | None = 160,
    content_hash: str = "h",
) -> Recipe:
    return Recipe(
        id=id,
        account_id=account_id,
        dockerfile="FROM mshkn-base",
        content_hash=content_hash,
        status=status,
        build_log=None,
        base_volume_id=base_volume_id,
        template_vmstate=None,
        template_memory=None,
        created_at="2026-03-08T00:00:00",
        built_at="2026-03-08T00:00:00" if status is RecipeStatus.READY else None,
    )


class ShellRecorder:
    """A stand-in for `mshkn.host.shell.run` that records and answers commands.

    `calls` holds `(cmd, check)` pairs. `ip link show <tap>` reports the taps in
    `taps` as present; every other command is matched against `responses` by
    substring, returning the value or raising it, and otherwise returns "".

    Pass `timeline` to interleave the commands with the caller's own events:
    every call appends `run:<cmd>` to that list, so a test can assert an
    ordering that spans shell commands and API or process activity.
    """

    def __init__(
        self,
        *,
        taps: set[str] | None = None,
        responses: dict[str, str | Exception] | None = None,
        timeline: list[str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.taps = set() if taps is None else taps
        self.responses = {} if responses is None else responses
        self.timeline = [] if timeline is None else timeline

    async def __call__(self, cmd: str, check: bool = True) -> str:
        self.calls.append((cmd, check))
        self.timeline.append(f"run:{cmd}")
        for key, resp in self.responses.items():
            if key in cmd:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        if cmd.startswith("ip link show "):
            tap = cmd.split()[3]
            return f"7: {tap}: <UP>" if tap in self.taps else ""
        return ""
