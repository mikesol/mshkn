from __future__ import annotations

import pytest

from mshkn.host.shell import ShellError, run


async def test_run_returns_stdout() -> None:
    assert await run("echo hi") == "hi\n"


async def test_run_raises_shell_error_with_code_and_stderr() -> None:
    with pytest.raises(ShellError) as info:
        await run("echo nope >&2; exit 3")
    assert (info.value.returncode, info.value.stderr) == (3, "nope\n")
    assert info.value.cmd == "echo nope >&2; exit 3"
    assert "Command failed (3)" in str(info.value)


async def test_run_check_false_returns_stdout_on_failure() -> None:
    assert await run("echo partial; exit 1", check=False) == "partial\n"
