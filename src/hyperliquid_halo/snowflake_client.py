"""Thin Snowflake connection helper.

Reads credentials from environment variables (``SNOWFLAKE_*``) so the same
CLI works with key-pair (RSA private key), password, or SSO
(``externalbrowser``) auth. Callers are expected to have loaded ``.env``
(e.g. via ``python-dotenv``) before instantiating this helper.

Key-pair auth is preferred when ``SNOWFLAKE_PRIVATE_KEY_PATH`` is set: the
file must be an RSA private key in PKCS#8 PEM form (``-----BEGIN PRIVATE
KEY-----`` or ``-----BEGIN ENCRYPTED PRIVATE KEY-----``), and the matching
public key must be registered on the Snowflake user
(``ALTER USER <user> SET RSA_PUBLIC_KEY='<base64 body>'``, an admin task).
See the README for generating the pair.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import snowflake.connector
from snowflake.connector import SnowflakeConnection
from snowflake.connector.cursor import SnowflakeCursor

logger = logging.getLogger(__name__)


# Role assumed when ``SNOWFLAKE_ROLE`` is unset: the read-only role for extraction work.
DEFAULT_ROLE = "DEV_READER"


class SnowflakeConfigError(RuntimeError):
    """Raised when required Snowflake environment variables are missing."""


def _key_pair_kwargs() -> dict[str, Any]:
    """Connection arguments for key-pair auth, or an empty dict when unset.

    Reads ``SNOWFLAKE_PRIVATE_KEY_PATH`` (``~`` is expanded) and the optional
    ``SNOWFLAKE_PRIVATE_KEY_PASSPHRASE`` for encrypted keys.

    Returns:
        ``{"private_key_file": ..., "private_key_file_pwd": ...}`` when a key
        path is configured, else ``{}``.

    Raises:
        SnowflakeConfigError: If the configured key file does not exist.
    """
    raw_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH", "").strip()
    if not raw_path:
        return {}
    key_path = os.path.expanduser(raw_path)
    if not os.path.isfile(key_path):
        raise SnowflakeConfigError(
            f"SNOWFLAKE_PRIVATE_KEY_PATH points to a missing file: {key_path}"
        )
    kwargs: dict[str, Any] = {"private_key_file": key_path}
    passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
    if passphrase:
        kwargs["private_key_file_pwd"] = passphrase
    return kwargs


def connection_kwargs() -> dict[str, Any]:
    """Build the ``snowflake.connector.connect`` arguments from the environment.

    Resolution order for authentication:

    1. ``SNOWFLAKE_PRIVATE_KEY_PATH`` set: key-pair (JWT) auth; no password
       is read even if ``SNOWFLAKE_PASSWORD`` is present.
    2. ``SNOWFLAKE_AUTHENTICATOR=externalbrowser``: Okta SSO, no password
       (the interim setup while the key-pair is unregistered).
    3. Otherwise: ``SNOWFLAKE_PASSWORD`` (required).

    Returns:
        Keyword arguments for the connector. Secrets are included, so never
        log the result.

    Raises:
        SnowflakeConfigError: If required environment variables are missing.
    """
    kwargs: dict[str, Any] = {
        "account": _required_env("SNOWFLAKE_ACCOUNT"),
        "user": _required_env("SNOWFLAKE_USER"),
        "database": os.environ.get("SNOWFLAKE_DATABASE", "ALLIUM_HYPERLIQUID"),
        "schema": os.environ.get("SNOWFLAKE_SCHEMA", "DEX"),
    }
    # Read-only role by default so extraction tooling never carries write rights.
    kwargs["role"] = os.environ.get("SNOWFLAKE_ROLE") or DEFAULT_ROLE
    if warehouse := os.environ.get("SNOWFLAKE_WAREHOUSE"):
        kwargs["warehouse"] = warehouse

    key_pair = _key_pair_kwargs()
    authenticator = os.environ.get("SNOWFLAKE_AUTHENTICATOR")
    if key_pair:
        kwargs.update(key_pair)
    elif authenticator:
        kwargs["authenticator"] = authenticator
        if authenticator.lower() == "externalbrowser":
            # Cache the SSO id token in the OS keychain so a loop of exports
            # (one connection per day) only asks for the browser login once.
            # Needs ALLOW_ID_TOKEN=true on the account; harmless otherwise.
            kwargs["client_store_temporary_credential"] = True
        else:
            kwargs["password"] = _required_env("SNOWFLAKE_PASSWORD")
    else:
        kwargs["password"] = _required_env("SNOWFLAKE_PASSWORD")
    return kwargs


def _required_env(name: str) -> str:
    """Return ``os.environ[name]`` or raise :class:`SnowflakeConfigError`."""
    value = os.environ.get(name)
    if not value:
        raise SnowflakeConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def connect() -> SnowflakeConnection:
    """Open a new Snowflake connection from environment variables.

    Supported env vars:
        SNOWFLAKE_ACCOUNT (required)
        SNOWFLAKE_USER (required)
        SNOWFLAKE_PRIVATE_KEY_PATH (key-pair auth; RSA PKCS#8 PEM, ``~`` ok)
        SNOWFLAKE_PRIVATE_KEY_PASSPHRASE (optional, for an encrypted key)
        SNOWFLAKE_PASSWORD (required only when no key path and no
            ``externalbrowser`` authenticator is set)
        SNOWFLAKE_ROLE (optional, defaults to DEV_READER)
        SNOWFLAKE_WAREHOUSE (optional)
        SNOWFLAKE_DATABASE (optional, defaults to ALLIUM_HYPERLIQUID)
        SNOWFLAKE_SCHEMA (optional, defaults to DEX)
        SNOWFLAKE_AUTHENTICATOR (optional, e.g. ``externalbrowser``)

    Returns:
        An open :class:`SnowflakeConnection`. Caller is responsible for
        closing it (prefer :func:`cursor` instead).

    Raises:
        SnowflakeConfigError: If required environment variables are missing.
    """
    kwargs = connection_kwargs()
    logger.info(
        "Connecting to Snowflake account=%s user=%s database=%s schema=%s auth=%s",
        kwargs["account"],
        kwargs["user"],
        kwargs["database"],
        kwargs["schema"],
        "key-pair" if "private_key_file" in kwargs
        else kwargs.get("authenticator", "password"),
    )
    return snowflake.connector.connect(**kwargs)


@contextmanager
def cursor() -> Iterator[SnowflakeCursor]:
    """Yield a Snowflake cursor and close the connection on exit.

    Yields:
        An open :class:`SnowflakeCursor`.

    Example:
        >>> with cursor() as cur:  # doctest: +SKIP
        ...     cur.execute("SELECT CURRENT_VERSION()")
        ...     print(cur.fetchone())
    """
    conn = connect()
    try:
        cur = conn.cursor()
        try:
            yield cur
        finally:
            cur.close()
    finally:
        conn.close()
