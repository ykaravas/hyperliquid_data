"""Unit tests for :mod:`hyperliquid_halo.snowflake_client` configuration.

Only the environment-to-kwargs resolution is tested; no connection is opened.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyperliquid_halo.snowflake_client import SnowflakeConfigError, connection_kwargs

_BASE = {"SNOWFLAKE_ACCOUNT": "acct", "SNOWFLAKE_USER": "svc", "SNOWFLAKE_WAREHOUSE": "wh"}


def _env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    for key in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD", "SNOWFLAKE_WAREHOUSE",
                "SNOWFLAKE_ROLE", "SNOWFLAKE_AUTHENTICATOR", "SNOWFLAKE_PRIVATE_KEY_PATH",
                "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", "SNOWFLAKE_DATABASE", "SNOWFLAKE_SCHEMA"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**_BASE, **extra}.items():
        monkeypatch.setenv(key, value)


def test_key_pair_takes_precedence_over_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key = tmp_path / "k.p8"
    key.write_text("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
    _env(monkeypatch, SNOWFLAKE_PASSWORD="stale", SNOWFLAKE_PRIVATE_KEY_PATH=str(key))
    kw = connection_kwargs()
    assert kw["private_key_file"] == str(key)
    assert "password" not in kw and "authenticator" not in kw
    assert "private_key_file_pwd" not in kw
    assert kw["database"] == "ALLIUM_HYPERLIQUID" and kw["schema"] == "DEX"


def test_key_pair_expands_tilde_and_passes_passphrase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key = tmp_path / "k.p8"
    key.write_text("x")
    monkeypatch.setenv("HOME", str(tmp_path))
    _env(monkeypatch, SNOWFLAKE_PRIVATE_KEY_PATH="~/k.p8", SNOWFLAKE_PRIVATE_KEY_PASSPHRASE="pw")
    kw = connection_kwargs()
    assert kw["private_key_file"] == str(key)
    assert kw["private_key_file_pwd"] == "pw"


def test_missing_key_file_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env(monkeypatch, SNOWFLAKE_PRIVATE_KEY_PATH=str(tmp_path / "nope.p8"))
    with pytest.raises(SnowflakeConfigError, match="missing file"):
        connection_kwargs()


def test_password_auth_when_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, SNOWFLAKE_PASSWORD="pw", SNOWFLAKE_ROLE="R")
    kw = connection_kwargs()
    assert kw["password"] == "pw" and kw["role"] == "R" and "private_key_file" not in kw


def test_external_browser_needs_no_password(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, SNOWFLAKE_AUTHENTICATOR="externalbrowser")
    kw = connection_kwargs()
    assert kw["authenticator"] == "externalbrowser" and "password" not in kw
    assert kw["client_store_temporary_credential"] is True


def test_password_required_without_key_or_sso(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    with pytest.raises(SnowflakeConfigError, match="SNOWFLAKE_PASSWORD"):
        connection_kwargs()


def test_role_defaults_to_dev_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SNOWFLAKE_ROLE`` unset falls back to the read-only ``DEV_READER`` role."""
    _env(monkeypatch, SNOWFLAKE_PASSWORD="pw")
    assert connection_kwargs()["role"] == "DEV_READER"
