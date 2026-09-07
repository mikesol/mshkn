"""The two ways mshkn is started: `python -m mshkn` and the ASGI module."""

from __future__ import annotations

import subprocess
import sys


def test_python_dash_m_mshkn_prints_help() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "mshkn", "--help"], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0
    assert "accounts" in proc.stdout
    assert "migrate" in proc.stdout


def test_asgi_entry_point_builds_the_app() -> None:
    from mshkn.main import app

    assert app.title == "mshkn"
    assert any(getattr(r, "path", "") == "/health" for r in app.routes)
