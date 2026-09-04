"""Test SSH module imports and types.

SSH functions require a real VM, so we only test that the module imports
correctly and the dataclass is well-formed.
"""

from mshkn.vm.ssh import ExecResult


def test_exec_result_fields() -> None:
    r = ExecResult(exit_code=0, stdout="hello\n", stderr="")
    assert r.exit_code == 0
    assert r.stdout == "hello\n"
    assert r.stderr == ""


def test_exec_result_nonzero() -> None:
    r = ExecResult(exit_code=1, stdout="", stderr="error\n")
    assert r.exit_code == 1
    assert r.stderr == "error\n"


def test_ssh_module_exports() -> None:
    from mshkn.vm.ssh import (
        ssh_download,
        ssh_exec,
        ssh_exec_bg,
        ssh_exec_stream,
        ssh_upload,
    )

    # Just verify they are callable
    assert callable(ssh_exec)
    assert callable(ssh_exec_stream)
    assert callable(ssh_exec_bg)
    assert callable(ssh_upload)
    assert callable(ssh_download)
