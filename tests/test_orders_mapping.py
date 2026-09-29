"""Unit tests for :mod:`hyperliquid_halo.orders_mapping`.

Exercises SQL assembly and ``OrdersQueryParams`` validation without
touching Snowflake.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from hyperliquid_halo.orders_mapping import (
    ALL_ORDER_COLUMNS,
    AUX_ORDER_COLUMNS,
    HALO_ORDER_COLUMNS,
    HIP3_DEX_QUOTE_TOKEN,
    LIST_ORDER_COINS_SQL,
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


def test_build_query_excludes_filled_vault_close_and_outcome_markets(
    base_params: OrdersQueryParams,
) -> None:
    """The §8 decisions in hl_orders_halo_mapping.md drop these at source."""
    sql, _ = build_orders_query(base_params)
    assert "STATUS != 'filled'" in sql
    assert "Vault Close" in sql  # filter clause references the literal
    # HIP-4 outcome markets (`#N` coins) are out of scope (§5.7).
    assert "AND COIN NOT LIKE '#%%'" in sql


def test_id_carries_side_suffix_and_rows_are_lifecycle_ordered(
    base_params: OrdersQueryParams,
) -> None:
    """Id matches production's execution OrderID form; same-ms rows are ranked (§2, §6)."""
    sql, _ = build_orders_query(base_params)
    assert "ORDER_ID || CASE SIDE WHEN 'B' THEN '-B' WHEN 'A' THEN '-S' END" in sql
    assert "AS Id," in sql
    order_by = sql[sql.rindex("ORDER BY"):]
    # The base column must be qualified: a bare STATUS in ORDER BY resolves to
    # the output alias `Status` (HALO values), which never equals 'open'.
    assert order_by.startswith(
        "ORDER BY TransactTime, Id, CASE WHEN o.STATUS = 'open' THEN 0"
    )
    assert "WHEN o.STATUS = 'triggered' THEN 1" in order_by
    assert "ELSE 2" in order_by


def test_cumqty_is_exact_decimal_and_text_is_raw_status(
    base_params: OrdersQueryParams,
) -> None:
    """CumQty avoids DOUBLE artifacts; Text carries the raw HL status (§6)."""
    sql, _ = build_orders_query(base_params)
    assert "TRY_CAST(ORIGINAL_SIZE AS NUMBER(38, 12)) - TRY_CAST(SIZE AS NUMBER(38, 12))" in sql
    assert "'[.]?0+$', ''" in sql  # fixed-scale rendering trimmed
    assert "AS DOUBLE" not in sql
    assert re.search(r"\n\s+STATUS\s+AS Text,", sql)
    assert HALO_ORDER_COLUMNS.index("Text") == HALO_ORDER_COLUMNS.index("SymbolType") + 1


def test_notional_is_size_times_limit_price_on_every_row(base_params: OrdersQueryParams) -> None:
    """§5.10: Notional in exact decimal, sent even where Price is withheld."""
    sql, _ = build_orders_query(base_params)
    size = "TRY_CAST(ORIGINAL_SIZE AS NUMBER(38, 12))"
    qty = f"COALESCE(IFF({size} = 0, ps.position_size, NULL), {size})"
    assert f"{qty} * TRY_CAST(LIMIT_PRICE AS NUMBER(38, 12))" in sql
    assert re.search(r"'\[\.\]\?0\+\$', ''\)\s+AS Notional,", sql)
    assert HALO_ORDER_COLUMNS.index("Notional") == HALO_ORDER_COLUMNS.index("Price") + 1


def test_full_position_stops_are_sized_from_the_position_lookback(
    base_params: OrdersQueryParams,
) -> None:
    """§5.8: zero-size rows take the trader's position at placement from DEX.TRADES."""
    sql, binds = build_orders_query(base_params)
    assert binds["position_lookup_start"] == datetime(2026, 3, 2, tzinfo=UTC)  # 30-day default
    assert "position_keys AS (" in sql and "position_fills AS (" in sql
    assert "position_sizes AS (" in sql
    assert "t.BUYER_START_POSITION::NUMBER(38, 12)" in sql
    assert "t.SELLER_START_POSITION::NUMBER(38, 12)" in sql
    assert "AND f.pf_ts <= k.pk_ts" in sql
    assert "WHERE rn = 1 AND pf_pos <> 0" in sql
    # Several fills can share a block timestamp; the block's tail wins the tie.
    assert "ARRAY_AGG(pf_start::VARIANT) OVER (PARTITION BY pf_user, pf_coin, pf_ts)" in sql
    assert "ORDER BY f.pf_ts DESC, f.pf_tail DESC" in sql
    assert "t.MARKET_TYPE = 'perpetuals'" in sql
    assert "LEFT JOIN position_sizes ps ON ps.ps_order_id = o.ORDER_ID" in sql
    # OrderQty / LeavesQty / Notional use the position only on zero-size rows.
    zero = "TRY_CAST(ORIGINAL_SIZE AS NUMBER(38, 12)) = 0"
    assert f"CASE WHEN {zero} AND ps.position_size IS NOT NULL" in sql
    assert "ELSE ORIGINAL_SIZE" in sql and "ELSE SIZE" in sql
    assert f"COALESCE(IFF({zero}, ps.position_size, NULL)" in sql
    assert "_PositionSize" in AUX_ORDER_COLUMNS
    with pytest.raises(ValueError, match="position_lookback_days"):
        OrdersQueryParams(
            start_ts=base_params.start_ts, end_ts=base_params.end_ts, position_lookback_days=-1
        )


def test_price_is_null_for_market_and_stoploss_rows(base_params: OrdersQueryParams) -> None:
    """Solidus instruction (2026-09-28): Price only on Limit / LimitToStop rows (§6)."""
    sql, _ = build_orders_query(base_params)
    assert "CASE WHEN TYPE IN ('Limit', 'Stop Limit', 'Take Profit Limit')" in sql
    assert "THEN LIMIT_PRICE" in sql
    assert "LIMIT_PRICE                                                        AS Price" not in sql


def test_oco_pairing_and_stoppx_come_from_the_armed_orders_lookback(
    base_params: OrdersQueryParams,
) -> None:
    """§5.2 pairing and §5.9 trigger-price lookback replace the position flag and IS_TRIGGER."""
    sql, binds = build_orders_query(base_params)
    assert binds["trigger_lookup_start"] == datetime(2026, 3, 18, tzinfo=UTC)  # 14-day default
    assert "armed_orders AS (" in sql
    assert "MAX_BY(TRIGGER_PRICE, STATUS_CHANGE_TIMESTAMP) AS trigger_px" in sql
    assert "MIN(ORDER_TIMESTAMP)                           AS placed_ts" in sql
    assert "AND ORDER_TIMESTAMP >= %(trigger_lookup_start)s" in sql
    assert "LEFT JOIN armed_orders ao ON ao.armed_order_id = o.ORDER_ID" in sql
    # OCO: pairing on the placement time, built from the lookback.
    assert "tpsl_pairs AS (" in sql
    assert "FROM armed_orders" in sql
    assert "HAVING COUNT_IF(armed_type LIKE 'Stop%%') > 0" in sql
    assert "AND COUNT_IF(armed_type LIKE 'Take Profit%%') > 0" in sql
    assert "AND tp.pair_ts   = COALESCE(ao.placed_ts, o.ORDER_TIMESTAMP)" in sql
    assert "CASE WHEN tp.pair_user IS NOT NULL" in sql
    assert "IS_TAKE_PROFIT_OR_STOP_LOSS THEN 'OCO'" not in sql
    # StopPx and OrigTransactTime survive the re-stamp; OrdType degrades without a price.
    assert re.search(r"THEN ao\.trigger_px\s+END\s+AS StopPx,", sql)
    assert "COALESCE(ao.placed_ts, ORDER_TIMESTAMP))::BIGINT" in sql
    assert "WHEN 'Stop Market' THEN IFF(ao.trigger_px IS NULL, 'Market', 'StopLoss')" in sql
    assert "WHEN 'Take Profit Limit' THEN IFF(ao.trigger_px IS NULL, 'Limit', 'LimitToStop')" in sql
    assert "FROM filtered o" in sql
    assert "LAST_VALUE" not in sql


def test_exclude_post_only_adds_the_tif_predicate(base_params: OrdersQueryParams) -> None:
    """Post-only quoting is dropped only on request; NULL-TIF trigger rows always stay."""
    sql, _ = build_orders_query(base_params)
    assert "TIME_IN_FORCE <> 'Alo'" not in sql
    params = OrdersQueryParams(
        start_ts=base_params.start_ts, end_ts=base_params.end_ts, exclude_post_only=True
    )
    sql, _ = build_orders_query(params)
    assert "AND (TIME_IN_FORCE IS NULL OR TIME_IN_FORCE <> 'Alo')" in sql
    # The predicate belongs to the day's rows, not to the armed_orders lookback.
    assert sql.index("TIME_IN_FORCE <> 'Alo'") < sql.index("armed_orders AS (")


def test_trigger_lookback_default_and_validation() -> None:
    params = OrdersQueryParams(
        start_ts=datetime(2026, 9, 20, tzinfo=UTC),
        end_ts=datetime(2026, 9, 21, tzinfo=UTC),
        trigger_lookback_days=3,
    )
    _, binds = build_orders_query(params)
    assert binds["trigger_lookup_start"] == datetime(2026, 9, 17, tzinfo=UTC)
    assert OrdersQueryParams(
        start_ts=datetime(2026, 9, 20, tzinfo=UTC),
        end_ts=datetime(2026, 9, 21, tzinfo=UTC),
    ).trigger_lookback_days == 14
    with pytest.raises(ValueError, match="trigger_lookback_days"):
        OrdersQueryParams(
            start_ts=datetime(2026, 9, 20, tzinfo=UTC),
            end_ts=datetime(2026, 9, 21, tzinfo=UTC),
            trigger_lookback_days=-1,
        )


def test_list_order_coins_labels_outcome_markets() -> None:
    """`#N` coins are reported but flagged as excluded from export-orders."""
    assert "WHEN COIN LIKE '#%%' THEN 'outcome (excluded)'" in LIST_ORDER_COINS_SQL


def test_build_query_omits_optional_filters_when_none(base_params: OrdersQueryParams) -> None:
    sql, binds = build_orders_query(base_params)
    assert "AND COIN = %(coin)s" not in sql  # the spot_pairs CTE has its own COIN predicate
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


def test_symbol_uses_dex_suffixed_hip3_form_and_per_dex_quote() -> None:
    """xyz:TSLA -> TSLA-XYZ/USDC, hyna:X -> X-HYNA/USDE; same symbology as executions."""
    sql, _ = build_orders_query(
        OrdersQueryParams(
            start_ts=datetime(2026, 4, 1, tzinfo=UTC),
            end_ts=datetime(2026, 4, 2, tzinfo=UTC),
        )
    )
    assert "WHEN COIN LIKE '%%:%%'" in sql
    assert "SPLIT_PART(COIN, ':', 2) || '-' || UPPER(SPLIT_PART(COIN, ':', 1)) || '/'" in sql
    assert "WHEN 'hyna' THEN 'USDE'" in sql
    assert "WHEN 'xyz' THEN 'USDC'" in sql
    assert "ELSE 'USDC'" in sql
    # Legacy base/quote spot passes through; plain perps and @N get /USDC.
    assert "WHEN COIN LIKE '%%/%%' THEN COIN" in sql
    assert "ELSE COIN || '/USDC'" in sql
    for dex, quote in HIP3_DEX_QUOTE_TOKEN.items():
        assert dex == dex.lower()
        assert quote.isupper()


def test_blockchain_and_symbol_type_match_executions_feed() -> None:
    sql, _ = build_orders_query(
        OrdersQueryParams(
            start_ts=datetime(2026, 4, 1, tzinfo=UTC),
            end_ts=datetime(2026, 4, 2, tzinfo=UTC),
        )
    )
    assert "'ethereum'                                                         AS Blockchain" in sql
    assert "'hyperliquid'" not in sql
    assert "symbol_type_map AS (" in sql
    assert "('BTC', 'Crypto')" in sql
    assert "LEFT JOIN symbol_type_map m ON m.map_coin = o.COIN" in sql
    assert "THEN 'Crypto' ELSE m.symbol_type END" in sql
    symbol_type_pos = HALO_ORDER_COLUMNS.index("SymbolType")
    assert symbol_type_pos == HALO_ORDER_COLUMNS.index("ContractMultiplier") + 1


def test_spot_pairs_resolved_from_dex_trades_with_lookback() -> None:
    """@N -> TOKEN_A/TOKEN_B via a DEX.TRADES lookback join; placeholder otherwise."""
    params = OrdersQueryParams(
        start_ts=datetime(2026, 9, 20, tzinfo=UTC),
        end_ts=datetime(2026, 9, 21, tzinfo=UTC),
        spot_lookback_days=7,
    )
    sql, binds = build_orders_query(params)
    assert binds["spot_lookup_start"] == datetime(2026, 9, 13, tzinfo=UTC)
    assert "spot_pairs AS (" in sql
    assert "FROM ALLIUM_HYPERLIQUID.DEX.TRADES" in sql
    assert "AND TIMESTAMP >= %(spot_lookup_start)s" in sql
    resolved = "COALESCE(sp.token_a || '/' || sp.token_b, COIN || '/USDC')"
    assert f"WHEN COIN LIKE '@%%' THEN {resolved}" in sql
    assert "'Hyperliquid:' || COALESCE(sp.pair, COIN)" in sql
    assert "LEFT JOIN spot_pairs sp ON sp.spot_coin = o.COIN" in sql
    # The @ branch must come before the HIP-3 ':' branch and the plain fallback.
    assert sql.index("WHEN COIN LIKE '@%%'") < sql.index("WHEN COIN LIKE '%%:%%'")


def test_spot_lookback_default_and_validation() -> None:
    params = OrdersQueryParams(
        start_ts=datetime(2026, 9, 20, tzinfo=UTC),
        end_ts=datetime(2026, 9, 21, tzinfo=UTC),
    )
    assert params.spot_lookback_days == 30
    with pytest.raises(ValueError, match="spot_lookback_days"):
        OrdersQueryParams(
            start_ts=datetime(2026, 9, 20, tzinfo=UTC),
            end_ts=datetime(2026, 9, 21, tzinfo=UTC),
            spot_lookback_days=-1,
        )


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
