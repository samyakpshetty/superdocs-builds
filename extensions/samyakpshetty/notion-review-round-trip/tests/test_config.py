from __future__ import annotations

import pytest

from notion_review.config import Config, ConfigError


def test_defaults_are_fake_and_frugal() -> None:
    cfg = Config.from_env({})
    assert cfg.provider == "fake"
    assert cfg.superdocs_base_url == "https://api.superdocs.app"
    assert cfg.sections_per_op == 25
    assert cfg.database_url is None


def test_live_requires_credentials() -> None:
    cfg = Config.from_env({"PROVIDER": "live"})
    with pytest.raises(ConfigError) as excinfo:
        cfg.require_live()
    assert "SUPERDOCS_API_KEY" in str(excinfo.value)
    assert "NOTION_TOKEN" in str(excinfo.value)


def test_live_with_credentials_validates() -> None:
    cfg = Config.from_env(
        {"PROVIDER": "live", "SUPERDOCS_API_KEY": "sk_x", "NOTION_TOKEN": "ntn_y"}
    )
    assert cfg.require_live() is cfg


def test_fake_never_requires_credentials() -> None:
    # require_live is a no-op for the fake provider, so a keyless run is always valid.
    assert Config.from_env({}).require_live().provider == "fake"


def test_invalid_provider_rejected() -> None:
    with pytest.raises(ConfigError):
        Config.from_env({"PROVIDER": "sqlite"})


def test_sample_size_parsed() -> None:
    assert Config.from_env({"SAMPLE_SIZE": "3"}).sample_size == 3
    assert Config.from_env({}).sample_size is None


def test_config_is_frozen() -> None:
    cfg = Config.from_env({})
    with pytest.raises(Exception):  # noqa: B017 - pydantic frozen raises ValidationError
        cfg.provider = "live"  # type: ignore[misc]
