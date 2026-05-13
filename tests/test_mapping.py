"""Unit tests for :mod:`hyperliquid_halo.mapping`.

These tests exercise SQL assembly and ``QueryParams`` validation without
touching Snowflake.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hyperliquid_halo.mapping import (
    ALL_COLUMNS,
    AUX_COLUMNS,
    HALO_COLUMNS,
    QueryParams,
    build_query,
)


@pytest.fixture()
def base_params() -> QueryParams:
    """A valid one-day UTC range with no market filters."""
    return QueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 2, tzinfo=UTC),
    )


def test_build_query_contains_both_sides(base_params: QueryParams) -> None:
    sql, binds = build_query(base_params)
    assert "buy_side" in sql
    assert "sell_side" in sql
    assert "UNION ALL" in sql
    assert binds["start_ts"] == base_params.start_ts
    assert binds["end_ts"] == base_params.end_ts


def test_build_query_omits_optional_filters_when_none(base_params: QueryParams) -> None:
    sql, binds = build_query(base_params)
    assert "AND COIN" not in sql
    assert "AND MARKET_TYPE" not in sql
    assert "coin" not in binds
    assert "market_type" not in binds


def test_build_query_adds_coin_and_market_type_filters() -> None:
    params = QueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 2, tzinfo=UTC),
        coin="BTC",
        market_type="perpetuals",
    )
    sql, binds = build_query(params)
    assert "AND COIN = %(coin)s" in sql
    assert "AND MARKET_TYPE = %(market_type)s" in sql
    assert binds["coin"] == "BTC"
    assert binds["market_type"] == "perpetuals"


def test_build_query_spot_filter() -> None:
    params = QueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 2, tzinfo=UTC),
        market_type="spot",
        token_a="HYPE",
    )
    sql, binds = build_query(params)
    assert binds["market_type"] == "spot"
    assert binds["token_a"] == "HYPE"
    assert "AND TOKEN_A_SYMBOL = %(token_a)s" in sql


def test_query_params_rejects_invalid_market_type() -> None:
    with pytest.raises(ValueError, match="market_type"):
        QueryParams(
            start_ts=datetime(2026, 4, 1, tzinfo=UTC),
            end_ts=datetime(2026, 4, 2, tzinfo=UTC),
            market_type="futures",
        )


def test_query_params_rejects_reversed_range() -> None:
    with pytest.raises(ValueError, match="strictly greater"):
        QueryParams(
            start_ts=datetime(2026, 4, 2, tzinfo=UTC),
            end_ts=datetime(2026, 4, 1, tzinfo=UTC),
        )


def test_column_lists_are_consistent() -> None:
    """``ALL_COLUMNS`` must equal HALO_COLUMNS + AUX_COLUMNS (minus Id)."""
    expected = HALO_COLUMNS + tuple(c for c in AUX_COLUMNS if c != "Id")
    assert ALL_COLUMNS == expected
    assert "Id" in HALO_COLUMNS
    assert "Id" in AUX_COLUMNS


def test_halo_columns_match_v21_required_set() -> None:
    """All HALO v2.1 required + required-if-EXCHANGE columns are present."""
    required = {
        "TransactTime", "Id", "OrderID", "ExecutionType", "Symbol", "Side",
        "Quantity", "Price", "Status", "Account", "ClientId", "OriginationTrader",
        "MatchingID", "MatchingOrderID", "MatchingAccount", "MatchingClientId",
        "MatchingOriginationTrader",
    }
    missing = required - set(HALO_COLUMNS)
    assert not missing, f"HALO_COLUMNS missing required fields: {missing}"
