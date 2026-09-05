"""ASGI entry point: `uvicorn mshkn.main:app`."""

from __future__ import annotations

from mshkn.app import create_app

app = create_app()
