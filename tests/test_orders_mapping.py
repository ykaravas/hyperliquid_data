"""Unit tests for :mod:`hyperliquid_halo.orders_mapping`.

Exercises SQL assembly and ``OrdersQueryParams`` validation without
touching Snowflake.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hyperliquid_halo.orders_mapping import (
    ALL_ORDER_COLUMNS,
    AUX_ORDER_COLUMNS,
    HALO_ORDER_COLUMNS,
    OrdersQueryParams,
    build_orders_query,
)


@pytest.fixture()
def base_params() -> OrdersQueryParams:
    """A valid one-day UTC range with no filters."""
    return OrdersQueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 2, tzinfo=UTC),
    )


def test_build_query_hits_orders_table(base_params: OrdersQueryParams) -> None:
    sql, binds = build_orders_query(base_params)
    assert "ALLIUM_HYPERLIQUID.RAW.ORDERS" in sql
    assert binds["start_ts"] == base_params.start_ts
    assert binds["end_ts"] == base_params.end_ts


def test_build_query_excludes_filled_and_vault_close(base_params: OrdersQueryParams) -> None:
    """The §8 decisions in hl_orders_halo_mapping.md drop these at source."""
    sql, _ = build_orders_query(base_params)
    assert "STATUS != 'filled'" in sql
    assert "Vault Close" in sql  # filter clause references the literal


def test_build_query_omits_optional_filters_when_none(base_params: OrdersQueryParams) -> None:
    sql, binds = build_orders_query(base_params)
    assert "AND COIN" not in sql
    assert "USER" not in binds  # user is the bind name; "USER" column always appears
    assert "coin" not in binds
    assert "market_type" not in binds


def test_build_query_adds_coin_filter() -> None:
    params = OrdersQueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 2, tzinfo=UTC),
        coin="BTC",
    )
    sql, binds = build_orders_query(params)
    assert "AND COIN = %(coin)s" in sql
    assert binds["coin"] == "BTC"


def test_build_query_market_type_spot_uses_coin_shape_inference() -> None:
    """RAW.ORDERS has no MARKET_TYPE column — spot is inferred from COIN."""
    params = OrdersQueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 2, tzinfo=UTC),
        market_type="spot",
    )
    sql, _ = build_orders_query(params)
    assert "COIN LIKE '@%'" in sql or "COIN LIKE '@%%'" in sql
    assert "AND (COIN LIKE" in sql


def test_build_query_market_type_perpetuals_negates_spot_shape() -> None:
    params = OrdersQueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 2, tzinfo=UTC),
        market_type="perpetuals",
    )
    sql, _ = build_orders_query(params)
    assert "NOT (COIN LIKE" in sql


def test_build_query_user_filter() -> None:
    params = OrdersQueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 2, tzinfo=UTC),
        user="0xdeadbeef",
    )
    sql, binds = build_orders_query(params)
    assert 'AND "USER" = %(user)s' in sql
    assert binds["user"] == "0xdeadbeef"


def test_query_params_rejects_invalid_market_type() -> None:
    with pytest.raises(ValueError, match="market_type"):
        OrdersQueryParams(
            start_ts=datetime(2026, 4, 1, tzinfo=UTC),
            end_ts=datetime(2026, 4, 2, tzinfo=UTC),
            market_type="futures",
        )


def test_query_params_rejects_reversed_range() -> None:
    with pytest.raises(ValueError, match="strictly greater"):
        OrdersQueryParams(
            start_ts=datetime(2026, 4, 2, tzinfo=UTC),
            end_ts=datetime(2026, 4, 1, tzinfo=UTC),
        )


def test_column_lists_are_consistent() -> None:
    """``ALL_ORDER_COLUMNS`` = HALO_ORDER_COLUMNS + (AUX cols not already in HALO)."""
    expected = HALO_ORDER_COLUMNS + tuple(
        c for c in AUX_ORDER_COLUMNS if c not in HALO_ORDER_COLUMNS
    )
    assert ALL_ORDER_COLUMNS == expected
    # Id and TransactTime are the join keys between halo and aux files.
    assert "Id" in HALO_ORDER_COLUMNS
    assert "Id" in AUX_ORDER_COLUMNS
    assert "TransactTime" in HALO_ORDER_COLUMNS
    assert "TransactTime" in AUX_ORDER_COLUMNS


def test_halo_order_columns_match_v21_required_set() -> None:
    """All HALO v2.1 required order columns are present."""
    required = {
        "TransactTime", "Id", "Symbol", "Side", "OrderQty", "OrdType",
        "Price", "Status", "OrderCapacity", "Account", "ClientId",
        "OriginationTrader",
    }
    missing = required - set(HALO_ORDER_COLUMNS)
    assert not missing, f"HALO_ORDER_COLUMNS missing required fields: {missing}"


def test_status_mapping_collapses_rejects_and_cancels() -> None:
    """Sanity-check the inline §4.4 mapping is present in the SQL."""
    sql, _ = build_orders_query(
        OrdersQueryParams(
            start_ts=datetime(2026, 4, 1, tzinfo=UTC),
            end_ts=datetime(2026, 4, 2, tzinfo=UTC),
        )
    )
    assert "WHEN STATUS = 'open' THEN 'New'" in sql
    assert "WHEN STATUS = 'triggered' THEN 'Replaced'" in sql
    # Literal '%' in LIKE patterns is doubled to '%%' so the Snowflake
    # pyformat preprocessor passes it through. After substitution Snowflake
    # sees '%Rejected', which is what we want.
    assert "WHEN STATUS LIKE '%%Rejected' THEN 'Rejected'" in sql
    # Side encoding: HL A/B → HALO Buy/Sell
    assert "WHEN 'B' THEN 'Buy'" in sql
    assert "WHEN 'A' THEN 'Sell'" in sql
    # ALO → PostOnly TrdType
    assert "TIME_IN_FORCE = 'Alo'" in sql
    assert "'PostOnly'" in sql
