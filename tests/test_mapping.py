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
    HALO_STRICT_COLUMNS,
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


def test_build_query_applies_production_eligibility_gate_by_default(
    base_params: QueryParams,
) -> None:
    """Default output must match what production ships: the R__07 gate is on."""
    sql, _ = build_query(base_params)
    assert "AND TOKEN_A_SYMBOL IS NOT NULL" in sql
    assert "AND TOKEN_B_SYMBOL IS NOT NULL" in sql
    # IS_HIP3 is NULL on spot rows; the predicate must be NULL-safe.
    assert "NOT (COALESCE(IS_HIP3, FALSE) AND PERP_DEX IS NULL)" in sql
    # Literal '%' is doubled for the pyformat preprocessor (see FUNCTIONAL_SPEC 2.2).
    assert "BUYER_DIR ILIKE '%%Liquidat%%' OR SELLER_DIR ILIKE '%%Liquidat%%'" in sql
    assert "'Auto-Deleveraging', 'Net Child Vaults', 'Spot Dust Conversion'" in sql


def test_build_query_include_ineligible_drops_the_gate(base_params: QueryParams) -> None:
    params = QueryParams(
        start_ts=base_params.start_ts,
        end_ts=base_params.end_ts,
        include_ineligible=True,
    )
    sql, _ = build_query(params)
    assert "ILIKE '%%Liquidat%%'" not in sql
    assert "Net Child Vaults" not in sql
    assert "TOKEN_A_SYMBOL IS NOT NULL" not in sql
    # The date range and the mapping itself are untouched.
    assert "TIMESTAMP >= %(start_ts)s" in sql
    assert "UNION ALL" in sql


def test_build_query_symbol_matches_production_forms(base_params: QueryParams) -> None:
    """Main-dex/spot: TOKEN_A/TOKEN_B. HIP-3: TOKEN_A-DEX/TOKEN_B (e.g. TSLA-XYZ/USDC)."""
    sql, _ = build_query(base_params)
    assert "e.TOKEN_A_SYMBOL || '-' || UPPER(e.PERP_DEX) || '/' || e.TOKEN_B_SYMBOL" in sql
    assert "ELSE e.TOKEN_A_SYMBOL || '/' || e.TOKEN_B_SYMBOL" in sql
    # No fabricated fallback symbol: unresolved symbols are gated out instead.
    assert "COALESCE(TOKEN_A_SYMBOL, COIN)" not in sql


def test_build_query_embeds_symbol_type_map(base_params: QueryParams) -> None:
    sql, _ = build_query(base_params)
    assert "symbol_type_map AS (" in sql
    assert "('BTC', 'Crypto')" in sql
    assert "LEFT JOIN symbol_type_map m ON m.map_coin = e.COIN" in sql
    # Spot is always Crypto; perps take the map value with no fallback.
    assert "WHEN e.MARKET_TYPE = 'spot' THEN 'Crypto'" in sql
    assert "ELSE m.symbol_type" in sql


def test_build_query_ids_and_order_ids(base_params: QueryParams) -> None:
    sql, _ = build_query(base_params)
    # Id / MatchingID come from the whitespace-free UNIQUE_ID key.
    assert "REPLACE(e.UNIQUE_ID, ' ', 'T') AS exec_key" in sql
    assert "exec_key || '-B'" in sql and "exec_key || '-S'" in sql
    # Default mode: OrderID stays the raw Hyperliquid order id so it joins to
    # halo_orders.csv Id. Production's '-B'/'-S' suffix is the strict mode.
    assert "BUYER_ORDER_ID::STRING || '-B'" not in sql
    assert "BUYER_ORDER_ID::STRING                                 AS OrderID" in sql
    assert "'ethereum'" in sql


def test_build_query_halo_strict_suffixes_order_ids(base_params: QueryParams) -> None:
    """--halo-strict reproduces production's side-suffixed OrderID / MatchingOrderID."""
    sql, _ = build_query(
        QueryParams(start_ts=base_params.start_ts, end_ts=base_params.end_ts, halo_strict=True)
    )
    # Buy side: own order -B, counterparty -S. Sell side: mirrored.
    assert "BUYER_ORDER_ID::STRING || '-B'                  AS OrderID" in sql
    assert "SELLER_ORDER_ID::STRING || '-S'              AS MatchingOrderID" in sql
    assert "SELLER_ORDER_ID::STRING || '-S'                  AS OrderID" in sql
    assert "BUYER_ORDER_ID::STRING || '-B'              AS MatchingOrderID" in sql
    # Everything else (gate, ids, symbols, map) is unchanged in strict mode.
    assert "AND TOKEN_A_SYMBOL IS NOT NULL" in sql
    assert "exec_key || '-B'" in sql
    assert "symbol_type_map AS (" in sql
    assert "{" not in sql and "}" not in sql


def test_halo_strict_columns_are_halo_columns_without_is_maker() -> None:
    assert HALO_STRICT_COLUMNS == tuple(c for c in HALO_COLUMNS if c != "IsMaker")
    assert "IsMaker" not in HALO_STRICT_COLUMNS
    assert HALO_STRICT_COLUMNS[-1] == "SymbolType"
    assert len(HALO_STRICT_COLUMNS) == 30


def test_query_params_rejects_strict_with_ineligible() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        QueryParams(
            start_ts=datetime(2026, 4, 1, tzinfo=UTC),
            end_ts=datetime(2026, 4, 2, tzinfo=UTC),
            halo_strict=True,
            include_ineligible=True,
        )


def test_halo_columns_follow_production_order_then_is_maker() -> None:
    assert HALO_COLUMNS.index("SymbolType") == HALO_COLUMNS.index("ContractMultiplier") + 1
    assert HALO_COLUMNS[-1] == "IsMaker"
    assert HALO_COLUMNS[:3] == ("TransactTime", "Id", "MatchingID")
    assert "_TradeId" in AUX_COLUMNS and "_SourceTradeId" in AUX_COLUMNS


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
