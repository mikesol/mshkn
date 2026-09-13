"""The membrane's settings come from /brain/.env (spec §8) and nowhere else."""

from __future__ import annotations

from pathlib import Path

import pytest
from membrane.config import DEFAULT_BRAIN, load_settings, parse_env
from membrane.effort import EFFORTS


def test_parse_env_reads_key_value_lines_and_ignores_comments() -> None:
    text = (
        "# the brain's keys\nMSHKN_API_URL=https://api.example\n\nMSHKN_API_KEY='mk-1'\nX=\"a=b\"\n"
    )
    assert parse_env(text) == {
        "MSHKN_API_URL": "https://api.example",
        "MSHKN_API_KEY": "mk-1",
        "X": "a=b",
    }


def test_load_settings_scripted_needs_no_model_keys(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "MSHKN_API_URL=http://flow\nMSHKN_API_KEY=mk-scoped\nMEMBRANE_MODEL=scripted\n"
    )
    settings = load_settings(tmp_path)
    assert settings.brain == tmp_path
    assert settings.api_url == "http://flow"
    assert settings.api_key == "mk-scoped"
    assert settings.model == "scripted"
    assert settings.model_id == "claude-opus-5"
    assert settings.anthropic_api_key is None and settings.openai_api_key is None


def test_load_settings_anthropic_requires_both_model_keys(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("MSHKN_API_URL=u\nMSHKN_API_KEY=k\nMEMBRANE_MODEL=anthropic\n")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        load_settings(tmp_path)
    (tmp_path / ".env").write_text(
        "MSHKN_API_URL=u\nMSHKN_API_KEY=k\nMEMBRANE_MODEL=anthropic\n"
        "ANTHROPIC_API_KEY=a\nOPENAI_API_KEY=o\nMEMBRANE_MODEL_ID=claude-opus-5\n"
    )
    settings = load_settings(tmp_path)
    assert settings.model == "anthropic" and settings.anthropic_api_key == "a"


def test_load_settings_rejects_unknown_model_and_missing_env(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path)
    (tmp_path / ".env").write_text("MSHKN_API_URL=u\nMSHKN_API_KEY=k\nMEMBRANE_MODEL=gpt\n")
    with pytest.raises(ValueError, match="MEMBRANE_MODEL"):
        load_settings(tmp_path)


def test_default_brain_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert Path("/brain") == DEFAULT_BRAIN
    (tmp_path / ".env").write_text("MSHKN_API_URL=u\nMSHKN_API_KEY=k\nMEMBRANE_MODEL=scripted\n")
    monkeypatch.setenv("MEMBRANE_BRAIN", str(tmp_path))
    assert load_settings().brain == tmp_path


def test_the_default_effort_is_optional_and_validated(tmp_path: Path) -> None:
    """`MEMBRANE_EFFORT` is the run's floor, not the value: the turn raises it (#122)."""
    base = "MSHKN_API_URL=u\nMSHKN_API_KEY=k\nMEMBRANE_MODEL=scripted\n"
    (tmp_path / ".env").write_text(base)
    assert load_settings(tmp_path).default_effort is None
    (tmp_path / ".env").write_text(base + "MEMBRANE_EFFORT=medium\n")
    assert load_settings(tmp_path).default_effort == "medium"
    (tmp_path / ".env").write_text(base + "MEMBRANE_EFFORT=turbo\n")
    with pytest.raises(ValueError, match="MEMBRANE_EFFORT"):
        load_settings(tmp_path)


def test_effort_off_is_not_a_rung_on_the_ladder(tmp_path: Path) -> None:
    """`output_config.effort` is Anthropic-specific. `off` is the absence of the
    axis, not the bottom of it, so it stays out of EFFORTS and travels as its own
    flag — `default_effort=None` already means "the API's default"."""
    base = "MSHKN_API_URL=u\nMSHKN_API_KEY=k\nMEMBRANE_MODEL=scripted\n"
    (tmp_path / ".env").write_text(base)
    settings = load_settings(tmp_path)
    assert settings.effort_enabled is True and settings.default_effort is None

    (tmp_path / ".env").write_text(base + "MEMBRANE_EFFORT=off\n")
    off = load_settings(tmp_path)
    assert off.effort_enabled is False and off.default_effort is None
    assert "off" not in EFFORTS


def test_the_model_base_url_defaults_to_anthropic_and_loses_its_trailing_slash(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text("MSHKN_API_URL=u\nMSHKN_API_KEY=k\nMEMBRANE_MODEL=scripted\n")
    assert load_settings(tmp_path).anthropic_base_url == "https://api.anthropic.com"
    (tmp_path / ".env").write_text(
        "MSHKN_API_URL=u\nMSHKN_API_KEY=k\nMEMBRANE_MODEL=scripted\n"
        "ANTHROPIC_BASE_URL=https://8000-comp-1.mshkn.dev/\n"
    )
    assert load_settings(tmp_path).anthropic_base_url == "https://8000-comp-1.mshkn.dev"


def test_body_extra_is_json_and_fails_at_load_not_at_the_relay(tmp_path: Path) -> None:
    base = "MSHKN_API_URL=u\nMSHKN_API_KEY=k\nMEMBRANE_MODEL=scripted\n"
    (tmp_path / ".env").write_text(base)
    assert load_settings(tmp_path).body_extra == {}

    (tmp_path / ".env").write_text(
        base + 'MEMBRANE_BODY_EXTRA={"providerOptions": {"gateway": {"only": ["anthropic"]}}}\n'
    )
    assert load_settings(tmp_path).body_extra == {
        "providerOptions": {"gateway": {"only": ["anthropic"]}}
    }

    (tmp_path / ".env").write_text(base + "MEMBRANE_BODY_EXTRA={not json\n")
    with pytest.raises(ValueError, match="MEMBRANE_BODY_EXTRA"):
        load_settings(tmp_path)

    (tmp_path / ".env").write_text(base + 'MEMBRANE_BODY_EXTRA=["a"]\n')
    with pytest.raises(ValueError, match="MEMBRANE_BODY_EXTRA"):
        load_settings(tmp_path)
