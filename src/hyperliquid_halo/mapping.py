"""SQL template that maps Allium ``ALLIUM_HYPERLIQUID.DEX.TRADES`` rows into
Solidus HALO v2.1 Execution Data records.

The source table stores one row per trade containing both sides. HALO expects
one execution record per side, so the query UNIONs a ``buy_side`` and a
``sell_side`` CTE. The output contains every HALO column (empty where the
source lacks data) plus leading-underscore ``_*`` columns that preserve
Hyperliquid-specific context for downstream surveillance. The exporter splits
those two groups into two CSV files (halo.csv + aux.csv) joined on ``Id``.

The mapping mirrors the production Snowflake view
``hyperliquid_v_linked_private_execution_v2`` (repo ``defi-hyperliquid-halo``,
``R__07_source_views.sql``) so that a CSV exported here carries the same
values production ships to HALO. Where this exporter deliberately differs,
``docs/hl_execs_halo_mapping.md`` section 5 records it.

Market handling:
    * ``SecurityType`` is ``SWAP`` for perpetuals and ``SPOT`` for spot;
      this is the authoritative perp-vs-spot distinction.
    * ``Symbol`` is ``<TOKEN_A_SYMBOL>/<TOKEN_B_SYMBOL>`` for main-dex perps
      and spot (``BTC/USDC``, ``HYPE/USDC``) and
      ``<TOKEN_A_SYMBOL>-<DEX>/<TOKEN_B_SYMBOL>`` for HIP-3 perps
      (``TSLA-XYZ/USDC``) so the same underlying on two builder dexes never
      collides.
    * ``PositionEffect`` and ``ContractMultiplier`` are emitted only for
      perpetuals; they are NULL for spot (PositionEffect is not applicable and
      HALO's ContractMultiplier requirement is SWAP/FUT/OPT/CFD-only).
    * ``SymbolType`` is ``Crypto`` for spot and the seeded per-market value
      (:mod:`hyperliquid_halo.symbol_type_map`) for perps; a perp missing
      from the map emits NULL rather than a guess.

Eligibility:
    By default the query applies production's eligibility gate (unresolved
    symbols, forced closures, vault aggregation rows and dust sweeps are
    dropped whole-trade). ``QueryParams.include_ineligible`` disables the
    gate for research exports; see ``_ELIGIBILITY_SQL``.

See ``docs/FUNCTIONAL_SPEC.md`` for the full field-by-field rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

from .symbol_type_map import render_values_rows

HALO_COLUMNS: tuple[str, ...] = (
    "TransactTime",
    "Id",
    "MatchingID",
    "OrderID",
    "MatchingOrderID",
    "ExecutionType",
    "Symbol",
    "Side",
    "Quantity",
    "Price",
    "Notional",
    "Status",
    "ExVenue",
    "Account",
    "MatchingAccount",
    "ClientId",
    "MatchingClientId",
    "OriginationTrader",
    "MatchingOriginationTrader",
    "OrderCapacity",
    "MatchingOrderCapacity",
    "TrdType",
    "ParentOrderId",
    "Blockchain",
    "WalletAddress",
    "SecurityType",
    "ExchangeSymbol",
    "PositionEffect",
    "ContractMultiplier",
    "SymbolType",
    "IsMaker",
)
"""HALO v2.1 columns emitted in the halo.csv file, in output order.

The order matches production's ``hyperliquid_v_linked_private_execution_v2``
column order (``TransactTime`` .. ``SymbolType``).

Note: ``IsMaker`` is not part of the HALO v2.1 spec; it's a Hyperliquid-
specific flag added at the user's request. HALO ignores unknown columns on
upload so including it here does not break ingestion.
"""


AUX_COLUMNS: tuple[str, ...] = (
    "Id",
    "_IsTaker",
    "_ClosedPnl",
    "_Fee",
    "_StartPosition",
    "_Direction",
    "_BuilderFee",
    "_BuilderAddress",
    "_LiquidatedUser",
    "_LiquidationMarkPrice",
    "_LiquidationMethod",
    "_TransactionHash",
    "_SourceTradeId",
    "_TradeId",
    "_MarketType",
    "_Coin",
    "_TokenA",
    "_TokenB",
    "_Pair",
    "_PerpDex",
    "_PerpMarketName",
    "_IsHip3",
)
"""Supplementary columns emitted in aux.csv (joined to halo.csv on Id)."""


ALL_COLUMNS: tuple[str, ...] = HALO_COLUMNS + AUX_COLUMNS[1:]
"""Full column list returned by the SQL (HALO cols + aux cols, deduped on Id)."""


HALO_STRICT_COLUMNS: tuple[str, ...] = tuple(c for c in HALO_COLUMNS if c != "IsMaker")
"""``halo.csv`` columns in HALO-strict mode (``QueryParams.halo_strict``):
production's ``hyperliquid_v_linked_private_execution_v2`` column set and
order exactly, i.e. :data:`HALO_COLUMNS` without the non-HALO ``IsMaker``
column. The maker flag is still available in aux as ``_IsTaker``.
"""


@dataclass(frozen=True)
class QueryParams:
    """Bind parameters for the mapping query.

    Attributes:
        start_ts: Inclusive lower bound on ``TIMESTAMP``. Naive datetimes are
            treated as UTC.
        end_ts: Exclusive upper bound on ``TIMESTAMP``.
        coin: Optional Allium ``COIN`` filter. For perpetuals this is the token
            symbol (``BTC``, ``ETH``) or the dex-prefixed HIP-3 name
            (``xyz:TSLA``); for spot it is a pair id (``@4``). When ``None``,
            no coin filter is applied.
        market_type: Optional ``'spot'`` or ``'perpetuals'`` filter. When
            ``None``, both market types are returned.
        token_a: Optional ``TOKEN_A_SYMBOL`` filter (useful for spot where the
            user thinks in token symbols rather than pair ids).
        token_b: Optional ``TOKEN_B_SYMBOL`` filter.
        include_ineligible: When ``True``, skip production's eligibility
            gate and return every trade in range, including liquidations,
            auto-deleveraging, vault aggregation rows, dust sweeps and
            trades with unresolved symbols. Default ``False`` matches what
            production ships to HALO.
        halo_strict: When ``True``, produce exactly what production ships:
            ``OrderID`` / ``MatchingOrderID`` carry the ``-B`` / ``-S`` side
            suffix and ``halo.csv`` drops the non-HALO ``IsMaker`` column
            (:data:`HALO_STRICT_COLUMNS`). Cannot be combined with
            ``include_ineligible``. Default ``False`` keeps raw order ids so
            executions join to this project's orders feed.
    """

    start_ts: datetime
    end_ts: datetime
    coin: str | None = None
    market_type: str | None = None
    token_a: str | None = None
    token_b: str | None = None
    include_ineligible: bool = False
    halo_strict: bool = False

    def __post_init__(self) -> None:
        if self.market_type is not None and self.market_type not in ("spot", "perpetuals"):
            raise ValueError(
                f"market_type must be 'spot' or 'perpetuals', got {self.market_type!r}"
            )
        if self.end_ts <= self.start_ts:
            raise ValueError(
                f"end_ts ({self.end_ts}) must be strictly greater than start_ts ({self.start_ts})"
            )
        if self.halo_strict and self.include_ineligible:
            raise ValueError(
                "halo_strict and include_ineligible are mutually exclusive: a strict file "
                "must contain only what production ships to HALO"
            )


def _as_utc(d: datetime | date) -> datetime:
    """Coerce a date/datetime to a UTC-aware datetime."""
    if isinstance(d, datetime):
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    return datetime.combine(d, time.min, tzinfo=UTC)


def build_query(params: QueryParams) -> tuple[str, dict[str, Any]]:
    """Render the mapping SQL and build its bind-parameter dict.

    Args:
        params: Query parameters (date range, optional market filters, and
            the eligibility switch).

    Returns:
        A ``(sql, binds)`` pair suitable for
        ``snowflake.connector.cursor.execute(sql, binds)``. ``sql`` uses
        ``pyformat`` placeholders (``%(name)s``); ``binds`` is a dict keyed by
        those placeholder names.

    Example:
        >>> from datetime import datetime, timezone
        >>> sql, binds = build_query(QueryParams(
        ...     start_ts=datetime(2026, 4, 1, tzinfo=timezone.utc),
        ...     end_ts=datetime(2026, 4, 2, tzinfo=timezone.utc),
        ...     coin="BTC",
        ...     market_type="perpetuals",
        ... ))
        >>> "UNION ALL" in sql
        True
    """
    binds: dict[str, Any] = {
        "start_ts": _as_utc(params.start_ts),
        "end_ts": _as_utc(params.end_ts),
    }

    filters: list[str] = []
    if params.coin is not None:
        filters.append("AND COIN = %(coin)s")
        binds["coin"] = params.coin
    if params.market_type is not None:
        filters.append("AND MARKET_TYPE = %(market_type)s")
        binds["market_type"] = params.market_type
    if params.token_a is not None:
        filters.append("AND TOKEN_A_SYMBOL = %(token_a)s")
        binds["token_a"] = params.token_a
    if params.token_b is not None:
        filters.append("AND TOKEN_B_SYMBOL = %(token_b)s")
        binds["token_b"] = params.token_b

    extra_filters = "\n      ".join(filters)
    eligibility = "" if params.include_ineligible else _ELIGIBILITY_SQL
    sql = _render_sql_template(strict=params.halo_strict).format(
        symbol_type_values=render_values_rows(),
        eligibility=eligibility,
        extra_filters=extra_filters,
    )
    return sql, binds


# Production's eligibility gate (hyperliquid_v_source_eligible, R__07), verbatim
# apart from the doubled '%%' that the Snowflake connector's pyformat
# preprocessor collapses back to '%'. A trade is dropped whole (both sides,
# preserving Buy/Sell parity) when:
#   1. its symbol is unresolved: TOKEN_A/TOKEN_B is NULL (Allium has not
#      mapped a new spot pair yet) or a HIP-3 row has no PERP_DEX;
#   2. either side is a forced closure: the liquidation family ('%Liquidat%'
#      catches 'Liquidated Cross/Isolated Long/Short' plus 'Partial Borrow
#      Liquidation' / 'Backstop Borrow Liquidation') or 'Auto-Deleveraging';
#   3. either side is not a real execution: 'Net Child Vaults' (vault
#      aggregation) or 'Spot Dust Conversion' (exchange dust sweep).
# IS_HIP3 is NULL on spot rows, so it must be COALESCEd: the un-coalesced form
# silently dropped every spot trade in production until 2026-08-24. NULL
# directions (Allium history through 2025-05-23) make the NOT(...) predicates
# evaluate NULL and are dropped too; that is intentional.
_ELIGIBILITY_SQL = """AND TOKEN_A_SYMBOL IS NOT NULL
      AND TOKEN_B_SYMBOL IS NOT NULL
      AND NOT (COALESCE(IS_HIP3, FALSE) AND PERP_DEX IS NULL)
      AND NOT (BUYER_DIR ILIKE '%%Liquidat%%' OR SELLER_DIR ILIKE '%%Liquidat%%')
      AND NOT (BUYER_DIR IN ('Auto-Deleveraging', 'Net Child Vaults', 'Spot Dust Conversion')
            OR SELLER_DIR IN ('Auto-Deleveraging', 'Net Child Vaults', 'Spot Dust Conversion'))"""


# The two CTEs are nearly identical; we build them from a shared template so
# the buy/sell mapping cannot drift out of sync.
def _side_cte(
    name: str,
    *,
    side_label: str,
    self_prefix: str,
    other_prefix: str,
    id_suffix: str,
    match_suffix: str,
    strict: bool,
) -> str:
    """Render one side of the mapping CTE.

    Args:
        name: CTE name (``buy_side`` / ``sell_side``).
        side_label: ``'Buy'`` or ``'Sell'``, the HALO ``Side`` enum value.
        self_prefix: Column prefix for the side being emitted (``BUYER`` / ``SELLER``).
        other_prefix: Column prefix for the counterparty (``SELLER`` / ``BUYER``).
        id_suffix: Suffix appended to the exec key for the HALO ``Id`` (``-B`` / ``-S``).
        match_suffix: Suffix for the counterparty ``MatchingID`` (``-S`` / ``-B``).
        strict: HALO-strict mode. When ``True`` the order ids carry the same
            side suffixes as ``Id`` / ``MatchingID`` (production form); when
            ``False`` they are the raw Hyperliquid order ids.

    Returns:
        A SQL fragment defining the named CTE.
    """
    if strict:
        order_id_lines = f"""        -- HALO-strict: production's side-suffixed order ids.
        {self_prefix}_ORDER_ID::STRING || '{id_suffix}'                  AS OrderID,
        {other_prefix}_ORDER_ID::STRING || '{match_suffix}'              AS MatchingOrderID,"""
    else:
        order_id_lines = f"""\
        -- Raw Hyperliquid order ids (no side suffix) so executions stay
        -- joinable to this project's orders feed (halo_orders.csv Id).
        -- Production appends '-B'/'-S'; --halo-strict does the same.
        {self_prefix}_ORDER_ID::STRING                                 AS OrderID,
        {other_prefix}_ORDER_ID::STRING                                AS MatchingOrderID,"""
    return f"""{name} AS (
    SELECT
        transact_time_ms                                               AS TransactTime,
        exec_key || '{id_suffix}'                                      AS Id,
        exec_key || '{match_suffix}'                                   AS MatchingID,
{order_id_lines}
        'EXCHANGE'                                                     AS ExecutionType,
        symbol_str                                                     AS Symbol,
        '{side_label}'                                                 AS Side,
        AMOUNT::STRING                                                 AS Quantity,
        PRICE::STRING                                                  AS Price,
        USD_AMOUNT::STRING                                             AS Notional,
        'Filled'                                                       AS Status,
        'Hyperliquid'                                                  AS ExVenue,
        {self_prefix}_ADDRESS                                          AS Account,
        {other_prefix}_ADDRESS                                         AS MatchingAccount,
        {self_prefix}_ADDRESS                                          AS ClientId,
        {other_prefix}_ADDRESS                                         AS MatchingClientId,
        {self_prefix}_ADDRESS                                          AS OriginationTrader,
        {other_prefix}_ADDRESS                                         AS MatchingOriginationTrader,
        'Agency'                                                       AS OrderCapacity,
        'Agency'                                                       AS MatchingOrderCapacity,
        'RegularTrade'                                                 AS TrdType,
        NULLIF({self_prefix}_TWAP_ID::STRING, '')                      AS ParentOrderId,
        -- HALO's Blockchain enum has no 'Hyperliquid' value; 'ethereum' is the
        -- closest valid one (HL accounts are EVM addresses).
        'ethereum'                                                     AS Blockchain,
        {self_prefix}_ADDRESS                                          AS WalletAddress,
        security_type_str                                              AS SecurityType,
        exchange_symbol_str                                            AS ExchangeSymbol,
        -- 'Settlement' is an exchange settlement of the position (delisting) -> CLOSE.
        -- Every other direction (flips, liquidations, ADL, dust conversions, vault
        -- aggregation) has no clean open/close semantics and stays NULL -- the empty
        -- value is preserved into halo.csv, never defaulted (HALO stores it as empty).
        CASE
            WHEN MARKET_TYPE = 'perpetuals' AND {self_prefix}_DIR IN ('Open Long', 'Open Short')
                THEN 'OPEN'
            WHEN MARKET_TYPE = 'perpetuals'
                AND {self_prefix}_DIR IN ('Close Long', 'Close Short', 'Settlement')
                THEN 'CLOSE'
        END                                                            AS PositionEffect,
        contract_multiplier_str                                        AS ContractMultiplier,
        symbol_type_str                                                AS SymbolType,

        -- Supplementary (aux) fields --------------------------------------
        NOT {self_prefix}_CROSSED                                      AS IsMaker,
        {self_prefix}_CROSSED                                          AS _IsTaker,
        {self_prefix}_CLOSED_PNL                                       AS _ClosedPnl,
        {self_prefix}_FEE                                              AS _Fee,
        {self_prefix}_START_POSITION                                   AS _StartPosition,
        {self_prefix}_DIR                                              AS _Direction,
        {self_prefix}_BUILDER_FEE                                      AS _BuilderFee,
        {self_prefix}_BUILDER_ADDRESS                                  AS _BuilderAddress,
        LIQUIDATED_USER                                                AS _LiquidatedUser,
        LIQUIDATION_MARK_PRICE                                         AS _LiquidationMarkPrice,
        LIQUIDATION_METHOD                                             AS _LiquidationMethod,
        TRANSACTION_HASH                                               AS _TransactionHash,
        exec_key                                                       AS _SourceTradeId,
        TRADE_ID                                                       AS _TradeId,
        MARKET_TYPE                                                    AS _MarketType,
        COIN                                                           AS _Coin,
        TOKEN_A_SYMBOL                                                 AS _TokenA,
        TOKEN_B_SYMBOL                                                 AS _TokenB,
        PAIR                                                           AS _Pair,
        PERP_DEX                                                       AS _PerpDex,
        PERP_MARKET_NAME                                               AS _PerpMarketName,
        IS_HIP3                                                        AS _IsHip3
    FROM base
)"""


def _render_sql_template(*, strict: bool) -> str:
    """Assemble the full mapping SQL for one mode.

    The result still carries the ``{symbol_type_values}``, ``{eligibility}``
    and ``{extra_filters}`` placeholders that :func:`build_query` fills in.
    ``base`` derives the per-trade values once (same shape as production's
    ``base`` CTE) so both side projections read identical Symbol /
    SecurityType / ExchangeSymbol / SymbolType values by construction.

    Args:
        strict: HALO-strict mode; see :func:`_side_cte`.

    Returns:
        The SQL template text.
    """
    buy_cte = _side_cte(
        "buy_side",
        side_label="Buy",
        self_prefix="BUYER",
        other_prefix="SELLER",
        id_suffix="-B",
        match_suffix="-S",
        strict=strict,
    )
    sell_cte = _side_cte(
        "sell_side",
        side_label="Sell",
        self_prefix="SELLER",
        other_prefix="BUYER",
        id_suffix="-S",
        match_suffix="-B",
        strict=strict,
    )
    return f"""WITH symbol_type_map AS (
    SELECT map_coin, symbol_type FROM VALUES
        {{symbol_type_values}}
    AS t(map_coin, symbol_type)
),
eligible AS (
    SELECT *
    FROM ALLIUM_HYPERLIQUID.DEX.TRADES
    WHERE TIMESTAMP >= %(start_ts)s
      AND TIMESTAMP <  %(end_ts)s
      {{eligibility}}
      {{extra_filters}}
),
base AS (
    SELECT
        e.*,
        DATE_PART(EPOCH_MILLISECOND, e.TIMESTAMP)::BIGINT AS transact_time_ms,
        -- Per-execution identity sent to HALO as Id/MatchingID (with a '-B'/'-S'
        -- side suffix). UNIQUE_ID (TRADE_ID-COIN-TIMESTAMP) is globally unique;
        -- TRADE_ID alone is only unique per coin and collides across markets.
        -- The single space is replaced with 'T' so the id carries no whitespace;
        -- this stays a 1:1 mapping back to the source UNIQUE_ID.
        REPLACE(e.UNIQUE_ID, ' ', 'T') AS exec_key,
        -- HIP-3 markets embed the dex so the same underlying on two builder
        -- dexes (xyz:TSLA vs cash:TSLA) never collides in HALO.
        CASE
            WHEN COALESCE(e.IS_HIP3, FALSE)
                THEN e.TOKEN_A_SYMBOL || '-' || UPPER(e.PERP_DEX) || '/' || e.TOKEN_B_SYMBOL
            ELSE e.TOKEN_A_SYMBOL || '/' || e.TOKEN_B_SYMBOL
        END AS symbol_str,
        CASE WHEN e.MARKET_TYPE = 'perpetuals' THEN 'SWAP' ELSE 'SPOT' END AS security_type_str,
        'Hyperliquid:' || COALESCE(NULLIF(e.PAIR, ''), e.COIN) AS exchange_symbol_str,
        CASE WHEN e.MARKET_TYPE = 'perpetuals' THEN '1' END AS contract_multiplier_str,
        -- SymbolType: spot is crypto-only on Hyperliquid, so spot rows are always
        -- 'Crypto'. Perps take the seeded per-market value and nothing else: a
        -- perp missing from the map ships with SymbolType empty (optional in
        -- HALO) until the map is regenerated. No guessed fallback.
        CASE
            WHEN e.MARKET_TYPE = 'spot' THEN 'Crypto'
            ELSE m.symbol_type
        END AS symbol_type_str
    FROM eligible e
    LEFT JOIN symbol_type_map m ON m.map_coin = e.COIN
),
{buy_cte},
{sell_cte}
SELECT * FROM buy_side
UNION ALL
SELECT * FROM sell_side
ORDER BY TransactTime, _SourceTradeId, Side
"""


LIST_MARKETS_SQL = """
SELECT
    MARKET_TYPE,
    COIN,
    PAIR,
    TOKEN_A_SYMBOL,
    TOKEN_B_SYMBOL,
    COUNT(*) AS trade_count
FROM ALLIUM_HYPERLIQUID.DEX.TRADES
WHERE TIMESTAMP >= %(start_ts)s
  AND TIMESTAMP <  %(end_ts)s
GROUP BY MARKET_TYPE, COIN, PAIR, TOKEN_A_SYMBOL, TOKEN_B_SYMBOL
ORDER BY trade_count DESC
"""
"""Summary query for ``list-markets``: all markets active in a date range."""
