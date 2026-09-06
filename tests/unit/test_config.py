from __future__ import annotations

from pathlib import Path

import pytest

from mshkn.config import Config, _parse
from mshkn.errors import ConfigError


def test_defaults_when_env_is_empty() -> None:
    cfg = Config.from_env({})
    assert cfg == Config()


def test_generic_names_map_every_field() -> None:
    cfg = Config.from_env(
        {
            "MSHKN_PORT": "9000",
            "MSHKN_DB_PATH": "/var/lib/mshkn/x.db",
            "MSHKN_THIN_POOL_NAME": "pool2",
            "MSHKN_THIN_VOLUME_SECTORS": "1234",
            "MSHKN_SSH_KEY_PATH": "/root/.ssh/other",
            "MSHKN_DOMAIN": "example.test",
        }
    )
    assert cfg.port == 9000
    assert cfg.db_path == Path("/var/lib/mshkn/x.db")
    assert cfg.thin_pool_name == "pool2"
    assert cfg.thin_volume_sectors == 1234
    assert cfg.ssh_key_path == Path("/root/.ssh/other")
    assert cfg.domain == "example.test"


def test_aliases_keep_working_and_win() -> None:
    cfg = Config.from_env(
        {
            "R2_ENDPOINT": "https://r2.example",
            "R2_ACCESS_KEY_ID": "k",
            "R2_SECRET_ACCESS_KEY": "s",
            "R2_BUCKET": "b",
            "MSHKN_IDLE_TIMEOUT": "120",
            "MSHKN_IDLE_TIMEOUT_SECONDS": "999",
            "MSHKN_CHECKPOINT_RETENTION": "5",
        }
    )
    assert cfg.r2_endpoint == "https://r2.example"
    assert cfg.r2_access_key_id == "k"
    assert cfg.r2_secret_access_key == "s"
    assert cfg.r2_bucket == "b"
    assert cfg.idle_timeout_seconds == 120
    assert cfg.checkpoint_retention_count == 5


@pytest.mark.parametrize(
    ("var", "value"),
    [("MSHKN_PORT", "eighty"), ("MSHKN_IDLE_TIMEOUT", "1.5"), ("MSHKN_THIN_VOLUME_SECTORS", "")],
)
def test_bad_values_raise_config_error_naming_the_variable(var: str, value: str) -> None:
    with pytest.raises(ConfigError, match=var):
        Config.from_env({var: value})


def test_unknown_mshkn_variables_are_ignored() -> None:
    assert Config.from_env({"MSHKN_NOT_A_FIELD": "1"}) == Config()


def test_parse_float_succeeds() -> None:
    assert _parse("X", "1.5", float) == 1.5


def test_parse_float_raises_config_error_naming_the_variable() -> None:
    with pytest.raises(ConfigError, match="X"):
        _parse("X", "one", float)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("No", False),
        ("off", False),
    ],
)
def test_bool_parsing(raw: str, expected: bool) -> None:
    assert _parse("MSHKN_X", raw, bool) is expected


def test_bool_rejects_other_words() -> None:
    with pytest.raises(ConfigError, match="MSHKN_X: expected a boolean"):
        _parse("MSHKN_X", "maybe", bool)


def test_empty_path_is_rejected_and_unknown_types_are_config_errors() -> None:
    with pytest.raises(ConfigError, match="empty path"):
        _parse("MSHKN_DB_PATH", "", Path)
    with pytest.raises(ConfigError, match="unsupported field type"):
        _parse("MSHKN_X", "1,2", list)
