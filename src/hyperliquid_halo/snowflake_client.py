"""Thin Snowflake connection helper.

Reads credentials from environment variables (``SNOWFLAKE_*``) so the same
CLI works with password, key-pair, or SSO (``externalbrowser``) auth. Callers
are expected to have loaded ``.env`` (e.g. via ``python-dotenv``) before
instantiating this helper.
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


class SnowflakeConfigError(RuntimeError):
    """Raised when required Snowflake environment variables are missing."""


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
        SNOWFLAKE_PASSWORD (required unless SNOWFLAKE_AUTHENTICATOR is set)
        SNOWFLAKE_ROLE (optional)
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
    kwargs: dict[str, Any] = {
        "account": _required_env("SNOWFLAKE_ACCOUNT"),
        "user": _required_env("SNOWFLAKE_USER"),
        "database": os.environ.get("SNOWFLAKE_DATABASE", "ALLIUM_HYPERLIQUID"),
        "schema": os.environ.get("SNOWFLAKE_SCHEMA", "DEX"),
    }
    if role := os.environ.get("SNOWFLAKE_ROLE"):
        kwargs["role"] = role
    if warehouse := os.environ.get("SNOWFLAKE_WAREHOUSE"):
        kwargs["warehouse"] = warehouse

    authenticator = os.environ.get("SNOWFLAKE_AUTHENTICATOR")
    if authenticator:
        kwargs["authenticator"] = authenticator
        if authenticator.lower() != "externalbrowser":
            kwargs["password"] = _required_env("SNOWFLAKE_PASSWORD")
    else:
        kwargs["password"] = _required_env("SNOWFLAKE_PASSWORD")

    logger.info(
        "Connecting to Snowflake account=%s user=%s database=%s schema=%s",
        kwargs["account"],
        kwargs["user"],
        kwargs["database"],
        kwargs["schema"],
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
