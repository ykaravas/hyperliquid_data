"""Shared SQL filter builders for the executions and orders mapping queries."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def coin_filter(coin: str | Sequence[str] | None) -> tuple[str | None, dict[str, Any]]:
    """Build the ``COIN`` predicate and its bind values for one or several markets.

    A single market keeps the historical ``COIN = %(coin)s`` form; several
    markets render ``COIN IN (%(coin_0)s, %(coin_1)s, ...)``. Values are always
    bound, never spliced into the SQL.

    Args:
        coin: One Allium ``COIN`` value (``BTC``, ``xyz:CL``, ``@107``), a
            sequence of them, or ``None`` / an empty sequence for no filter.

    Returns:
        ``(predicate, binds)``: the ``AND ...`` clause (``None`` when there is
        no filter) and the pyformat bind values it needs.

    Raises:
        ValueError: If a market name in the sequence is empty.

    Example:
        >>> coin_filter("BTC")
        ('AND COIN = %(coin)s', {'coin': 'BTC'})
        >>> coin_filter(["xyz:CL", "xyz:NATGAS"])[0]
        'AND COIN IN (%(coin_0)s, %(coin_1)s)'
    """
    if coin is None:
        return None, {}
    coins = [coin] if isinstance(coin, str) else list(coin)
    if not coins:
        return None, {}
    if any(not c for c in coins):
        raise ValueError(f"coin filter holds an empty market name: {coins!r}")
    if len(coins) == 1:
        return "AND COIN = %(coin)s", {"coin": coins[0]}
    binds = {f"coin_{i}": c for i, c in enumerate(coins)}
    placeholders = ", ".join(f"%({name})s" for name in binds)
    return f"AND COIN IN ({placeholders})", binds
