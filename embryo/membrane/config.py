"""Settings from /brain/.env (spec §8). The file holds the scoped key, the API
URL, which model to run and, until #92, the two model keys."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

DEFAULT_BRAIN = Path("/brain")
DEFAULT_MODEL_ID = "claude-opus-5"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"

ModelKind = Literal["anthropic", "scripted"]
EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class Settings:
    brain: Path
    api_url: str
    api_key: str
    model: ModelKind
    model_id: str
    anthropic_api_key: str | None
    openai_api_key: str | None
    # `output_config.effort` for every completion; None leaves the API's default (high).
    # The measure sets it per run (#106): at the default, Opus 5 can think for the whole
    # 240 s turn on the hard turns.
    effort: str | None = None
    # Where the relay forwards a model call (spec relay design §4): the real
    # Anthropic API by default, overridden in the measure to point at a fake.
    anthropic_base_url: str = DEFAULT_ANTHROPIC_BASE_URL


def parse_env(text: str) -> dict[str, str]:
    """KEY=value lines; blank lines and # comments skipped; one layer of quotes removed."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def brain_dir() -> Path:
    override = os.environ.get("MEMBRANE_BRAIN")
    return Path(override) if override else DEFAULT_BRAIN


def load_settings(brain: Path | None = None) -> Settings:
    root = brain if brain is not None else brain_dir()
    env = parse_env((root / ".env").read_text())
    model = env.get("MEMBRANE_MODEL", "anthropic")
    if model not in ("anthropic", "scripted"):
        raise ValueError(f"MEMBRANE_MODEL must be anthropic or scripted, not {model!r}")
    anthropic_key = env.get("ANTHROPIC_API_KEY") or None
    openai_key = env.get("OPENAI_API_KEY") or None
    effort = env.get("MEMBRANE_EFFORT") or None
    if effort is not None and effort not in EFFORTS:
        raise ValueError(f"MEMBRANE_EFFORT must be one of {', '.join(EFFORTS)}, not {effort!r}")
    if model == "anthropic":
        for name, value in (("ANTHROPIC_API_KEY", anthropic_key), ("OPENAI_API_KEY", openai_key)):
            if value is None:
                raise ValueError(f"{name} is required when MEMBRANE_MODEL=anthropic")
    return Settings(
        brain=root,
        api_url=env["MSHKN_API_URL"],
        api_key=env["MSHKN_API_KEY"],
        model=cast("ModelKind", model),
        model_id=env.get("MEMBRANE_MODEL_ID", DEFAULT_MODEL_ID),
        anthropic_api_key=anthropic_key,
        openai_api_key=openai_key,
        effort=effort,
        anthropic_base_url=env.get("ANTHROPIC_BASE_URL", DEFAULT_ANTHROPIC_BASE_URL).rstrip("/"),
    )
