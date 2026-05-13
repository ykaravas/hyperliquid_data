"""SQL template that maps Allium ``ALLIUM_HYPERLIQUID.DEX.TRADES`` rows into
Solidus HALO v2.1 Execution Data records.

The source table stores one row per trade containing both sides. HALO expects
one execution record per side, so the query UNIONs a ``buy_side`` and a
``sell_side`` CTE. The output contains every HALO column (empty where the
source lacks data) plus leading-underscore ``_*`` columns that preserve
Hyperliquid-specific context for downstream surveillance. The exporter splits
those two groups into two CSV files (halo.csv + aux.csv) joined on ``Id``.

Market handling:
    * ``SecurityType`` is ``SWAP`` for perpetuals and ``SPOT`` for spot —
      this is the authoritative perp-vs-spot distinction.
    * ``Symbol`` is ``<TOKEN_A_SYMBOL>/<TOKEN_B_SYMBOL>`` for both market
      types (e.g. ``BTC/USDC`` for BTC perps, ``UBTC/USDC`` for Unit BTC
      spot). Falls back to ``COIN`` when token symbols are missing.
    * ``PositionEffect`` and ``ContractMultiplier`` are emitted only for
      perpetuals; they are NULL for spot (PositionEffect is not applicable and
      HALO's ContractMultiplier requirement is SWAP/FUT/OPT/CFD-only).

See ``docs/FUNCTIONAL_SPEC.md`` for the full field-by-field rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

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
    "IsMaker",
)
"""HALO v2.1 columns emitted in the halo.csv file, in output order.

Note: ``IsMaker`` is not part of the HALO v2.1 spec — it's a Hyperliquid-
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


@dataclass(frozen=True)
class QueryParams:
    """Bind parameters for the mapping query.

    Attributes:
        start_ts: Inclusive lower bound on ``TIMESTAMP``. Naive datetimes are
            treated as UTC.
        end_ts: Exclusive upper bound on ``TIMESTAMP``.
        coin: Optional Allium ``COIN`` filter. For perpetuals this is the token
            symbol (``BTC``, ``ETH``); for spot it is a pair id (``@4``). When
            ``None``, no coin filter is applied.
        market_type: Optional ``'spot'`` or ``'perpetuals'`` filter. When
            ``None``, both market types are returned.
        token_a: Optional ``TOKEN_A_SYMBOL`` filter (useful for spot where the
            user thinks in token symbols rather than pair ids).
        token_b: Optional ``TOKEN_B_SYMBOL`` filter.
    """

    start_ts: datetime
    end_ts: datetime
    coin: str | None = None
    market_type: str | None = None
    token_a: str | None = None
    token_b: str | None = None

    def __post_init__(self) -> None:
        if self.market_type is not None and self.market_type not in ("spot", "perpetuals"):
            raise ValueError(
                f"market_type must be 'spot' or 'perpetuals', got {self.market_type!r}"
            )
        if self.end_ts <= self.start_ts:
            raise ValueError(
                f"end_ts ({self.end_ts}) must be strictly greater than start_ts ({self.start_ts})"
            )


def _as_utc(d: datetime | date) -> datetime:
    """Coerce a date/datetime to a UTC-aware datetime."""
    if isinstance(d, datetime):
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    return datetime.combine(d, time.min, tzinfo=UTC)


def build_query(params: QueryParams) -> tuple[str, dict[str, Any]]:
    """Render the mapping SQL and build its bind-parameter dict.

    Args:
        params: Query parameters (date range and optional market filters).

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

    extra_filters = "\n          ".join(filters)
    sql = _SQL_TEMPLATE.format(extra_filters=extra_filters)
    return sql, binds


# The two CTEs are nearly identical — we build them from a shared template so
# the buy/sell mapping cannot drift out of sync.
def _side_cte(
    name: str,
    *,
    side_label: str,
    self_prefix: str,
    other_prefix: str,
    id_suffix: str,
    match_suffix: str,
) -> str:
    """Render one side of the mapping CTE.

    Args:
        name: CTE name (``buy_side`` / ``sell_side``).
        side_label: ``'Buy'`` or ``'Sell'`` — the HALO ``Side`` enum value.
        self_prefix: Column prefix for the side being emitted (``BUYER`` / ``SELLER``).
        other_prefix: Column prefix for the counterparty (``SELLER`` / ``BUYER``).
        id_suffix: Suffix appended to ``TRADE_ID`` for the HALO ``Id`` (``-B`` / ``-S``).
        match_suffix: Suffix for the counterparty ``MatchingID`` (``-S`` / ``-B``).

    Returns:
        A SQL fragment defining the named CTE.
    """
    return f"""{name} AS (
    SELECT
        DATE_PART(EPOCH_MILLISECOND, TIMESTAMP)::BIGINT                AS TransactTime,
        TRADE_ID || '{id_suffix}'                                      AS Id,
        TRADE_ID || '{match_suffix}'                                   AS MatchingID,
        {self_prefix}_ORDER_ID::STRING                                 AS OrderID,
        {other_prefix}_ORDER_ID::STRING                                AS MatchingOrderID,
        'EXCHANGE'                                                     AS ExecutionType,
        COALESCE(TOKEN_A_SYMBOL, COIN) || '/' || COALESCE(TOKEN_B_SYMBOL, 'USDC')
                                                                       AS Symbol,
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
        CASE
            WHEN {self_prefix}_TWAP_ID IS NOT NULL AND {self_prefix}_TWAP_ID != ''
            THEN {self_prefix}_TWAP_ID::STRING
        END                                                            AS ParentOrderId,
        'Hyperliquid'                                                  AS Blockchain,
        {self_prefix}_ADDRESS                                          AS WalletAddress,
        CASE WHEN MARKET_TYPE = 'perpetuals' THEN 'SWAP' ELSE 'SPOT' END AS SecurityType,
        'Hyperliquid:' || COALESCE(PAIR, COIN)                         AS ExchangeSymbol,
        CASE
            WHEN MARKET_TYPE = 'perpetuals' AND {self_prefix}_DIR IN ('Open Long', 'Open Short')
                THEN 'OPEN'
            WHEN MARKET_TYPE = 'perpetuals' AND {self_prefix}_DIR IN ('Close Long', 'Close Short')
                THEN 'CLOSE'
        END                                                            AS PositionEffect,
        CASE WHEN MARKET_TYPE = 'perpetuals' THEN '1' END               AS ContractMultiplier,

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
        TRADE_ID                                                       AS _SourceTradeId,
        MARKET_TYPE                                                    AS _MarketType,
        COIN                                                           AS _Coin,
        TOKEN_A_SYMBOL                                                 AS _TokenA,
        TOKEN_B_SYMBOL                                                 AS _TokenB,
        PAIR                                                           AS _Pair,
        PERP_DEX                                                       AS _PerpDex,
        PERP_MARKET_NAME                                               AS _PerpMarketName,
        IS_HIP3                                                        AS _IsHip3
    FROM filtered
)"""


_BUY_CTE = _side_cte(
    "buy_side",
    side_label="Buy",
    self_prefix="BUYER",
    other_prefix="SELLER",
    id_suffix="-B",
    match_suffix="-S",
)

_SELL_CTE = _side_cte(
    "sell_side",
    side_label="Sell",
    self_prefix="SELLER",
    other_prefix="BUYER",
    id_suffix="-S",
    match_suffix="-B",
)


_SQL_TEMPLATE = f"""WITH filtered AS (
    SELECT *
    FROM ALLIUM_HYPERLIQUID.DEX.TRADES
    WHERE TIMESTAMP >= %(start_ts)s
      AND TIMESTAMP <  %(end_ts)s
      {{extra_filters}}
),
{_BUY_CTE},
{_SELL_CTE}
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
