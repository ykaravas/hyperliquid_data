"""SQL template that maps Allium ``ALLIUM_HYPERLIQUID.RAW.ORDERS`` rows into
Solidus HALO v2.1 Order Data records.

The source table is event-sourced (one row per status change, not one row per
order), so the mapping emits one HALO order row per source row. Multiple
HALO rows for the same Hyperliquid ``oid`` share the same ``Id`` — HALO
models status transitions of a single order this way.

See ``docs/hl_orders_halo_mapping.md`` for the full field-by-field
rationale, including the §8 decisions log that drives:

* ``filled`` order rows are filtered out (fills are covered by the trades
  pipeline; HALO order ``Status`` does not accept ``Filled`` regardless).
* ``Vault Close`` order rows are filtered out (vaults out of scope today).
* ``triggered`` → HALO ``Replaced``.
* ``Alo`` (post-only) → ``TimeInForce=GoodTillCancel`` + ``TrdType=PostOnly``.
* HL ``A``/``B`` side encoding mapped to HALO ``Buy``/``Sell``.
* ``Stop Market`` and ``Take Profit Market`` both map to ``StopLoss`` (HALO
  has no take-profit enum); ``Stop Limit`` and ``Take Profit Limit`` both
  map to ``LimitToStop``. Direction is implicit in ``StopPx`` vs. mid.
* HIP-3 ``xyz:*`` identifiers are kept verbatim in ``Symbol`` to avoid
  collisions across builder-deployed perp dexes.
* ``@N`` spot pair identifiers emit ``{COIN}/USDC`` as a placeholder.
  Real ``@N → base`` resolution is deferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

HALO_ORDER_COLUMNS: tuple[str, ...] = (
    "TransactTime",
    "Id",
    "Symbol",
    "Side",
    "OrderQty",
    "OrdType",
    "Price",
    "Status",
    "OrderCapacity",
    "Account",
    "ClientId",
    "OriginationTrader",
    "StopPx",
    "CumQty",
    "LeavesQty",
    "TimeInForce",
    "OrigTransactTime",
    "ContingencyType",
    "ExVenue",
    "TrdType",
    "Blockchain",
    "WalletAddress",
    "SecurityType",
    "ExchangeSymbol",
    "ContractMultiplier",
)
"""HALO v2.1 order columns emitted in halo_orders.csv, in output order.

Always-NULL fields (``Notional``, ``ExpireDateTime``, ``BUIdentifier``,
``IpAddress``, ``ParentOrderId``) are intentionally omitted: HL has no
source data for them, and the HALO uploader can otherwise parse empty
columns as literal ``null`` strings.
"""


AUX_ORDER_COLUMNS: tuple[str, ...] = (
    "Id",
    "TransactTime",
    "_UniqueId",
    "_RawStatus",
    "_RawSide",
    "_RawType",
    "_RawTif",
    "_Coin",
    "_ClientOrderId",
    "_IsTrigger",
    "_IsTpSl",
    "_IsReduceOnly",
    "_TriggerCondition",
    "_TriggerPrice",
    "_Children",
    "_BuilderAddress",
    "_BuilderFee",
    "_OrderTimestamp",
    "_StatusChangeTimestamp",
)
"""Supplementary columns in aux_orders.csv. Joined to halo_orders.csv on
``(Id, TransactTime)`` — a single HL ``oid`` can appear in multiple HALO
rows (one per status event), so ``Id`` alone is insufficient.
"""


ALL_ORDER_COLUMNS: tuple[str, ...] = HALO_ORDER_COLUMNS + tuple(
    c for c in AUX_ORDER_COLUMNS if c not in HALO_ORDER_COLUMNS
)
"""Full column list returned by the SQL (HALO cols + aux cols, deduped on the
shared ``Id`` and ``TransactTime``).
"""


@dataclass(frozen=True)
class OrdersQueryParams:
    """Bind parameters for the orders mapping query.

    Attributes:
        start_ts: Inclusive lower bound on ``ORDER_TIMESTAMP``. Naive
            datetimes are treated as UTC.
        end_ts: Exclusive upper bound on ``ORDER_TIMESTAMP``.
        coin: Optional Allium ``COIN`` filter. Plain perpetual names
            (``BTC``), k-prefix (``kPEPE``), HIP-3 (``xyz:SP500``), spot
            indices (``@107``), and legacy spot pairs (``PURR/USDC``) are
            all valid values for this field.
        market_type: Optional ``'spot'`` / ``'perpetuals'`` filter.
            ``RAW.ORDERS`` has no ``MARKET_TYPE`` column, so the filter
            is inferred from ``COIN`` shape: ``@N``-prefixed or
            ``base/quote`` → spot; everything else → perpetuals.
        user: Optional on-chain address filter (matches the ``USER``
            column). Useful for per-account analysis.
    """

    start_ts: datetime
    end_ts: datetime
    coin: str | None = None
    market_type: str | None = None
    user: str | None = None

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


def build_orders_query(params: OrdersQueryParams) -> tuple[str, dict[str, Any]]:
    """Render the orders mapping SQL and build its bind-parameter dict.

    Args:
        params: Query parameters (date range and optional filters).

    Returns:
        A ``(sql, binds)`` pair suitable for
        ``snowflake.connector.cursor.execute(sql, binds)``. ``sql`` uses
        ``pyformat`` placeholders (``%(name)s``); ``binds`` is keyed by
        those placeholder names.

    Example:
        >>> from datetime import datetime, timezone
        >>> sql, binds = build_orders_query(OrdersQueryParams(
        ...     start_ts=datetime(2026, 4, 1, tzinfo=timezone.utc),
        ...     end_ts=datetime(2026, 4, 2, tzinfo=timezone.utc),
        ...     coin="BTC",
        ... ))
        >>> "ALLIUM_HYPERLIQUID.RAW.ORDERS" in sql
        True
        >>> "STATUS = 'filled'" not in sql  # filled rows excluded
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
    if params.market_type == "spot":
        filters.append("AND (COIN LIKE '@%%' OR COIN LIKE '%%/%%')")
    elif params.market_type == "perpetuals":
        filters.append("AND NOT (COIN LIKE '@%%' OR COIN LIKE '%%/%%')")
    if params.user is not None:
        filters.append('AND "USER" = %(user)s')
        binds["user"] = params.user

    extra_filters = "\n          ".join(filters)
    sql = _SQL_TEMPLATE.format(extra_filters=extra_filters)
    return sql, binds


# Symbol derivation: see §3 of hl_orders_halo_mapping.md.
# Plain perps (BTC), k-prefix (kPEPE), HIP-3 (xyz:SP500) → `{COIN}/USDC`.
# Spot pairs already containing `/` (PURR/USDC) → pass through.
# @N spot indices → `{COIN}/USDC` placeholder (real resolution deferred).
# Note: literal `%` is doubled to `%%` so the Snowflake connector's pyformat
# preprocessor passes it through (single `%` triggers placeholder substitution).
_SYMBOL_EXPR = """CASE
            WHEN COIN LIKE '%%/%%' THEN COIN
            ELSE COIN || '/USDC'
        END"""

# SecurityType: spot when COIN is `@N` or already contains `/`; perp otherwise.
_IS_SPOT_EXPR = "(COIN LIKE '@%%' OR COIN LIKE '%%/%%')"

# Status mapping — see §4.4. Anything ending in `Rejected` is Rejected;
# `open` is New; `triggered` is Replaced; anything else is Canceled
# (covers `canceled`, `*Canceled`, `scheduledCancel`).
_STATUS_EXPR = """CASE
            WHEN STATUS = 'open' THEN 'New'
            WHEN STATUS = 'triggered' THEN 'Replaced'
            WHEN STATUS LIKE '%%Rejected' THEN 'Rejected'
            ELSE 'Canceled'
        END"""

# OrdType mapping — see §4.2. Stop Market and Take Profit Market both map
# to StopLoss; Stop Limit and Take Profit Limit both map to LimitToStop.
_ORDTYPE_EXPR = """CASE TYPE
            WHEN 'Limit' THEN 'Limit'
            WHEN 'Market' THEN 'Market'
            WHEN 'Stop Market' THEN 'StopLoss'
            WHEN 'Take Profit Market' THEN 'StopLoss'
            WHEN 'Stop Limit' THEN 'LimitToStop'
            WHEN 'Take Profit Limit' THEN 'LimitToStop'
        END"""

# TIF mapping — see §4.3. Alo carries post-only intent via TrdType; the TIF
# itself collapses to GTC. FrontendMarket and LiquidationMarket map to IOC.
# Empty TIF (trigger orders) defaults to GTC.
_TIF_EXPR = """CASE TIME_IN_FORCE
            WHEN 'Alo' THEN 'GoodTillCancel'
            WHEN 'Gtc' THEN 'GoodTillCancel'
            WHEN 'Ioc' THEN 'ImmediateOrCancel'
            WHEN 'FrontendMarket' THEN 'ImmediateOrCancel'
            WHEN 'LiquidationMarket' THEN 'ImmediateOrCancel'
            ELSE 'GoodTillCancel'
        END"""

# TrdType: PostOnly only when TIF is Alo, otherwise RegularTrade.
_TRDTYPE_EXPR = """CASE WHEN TIME_IN_FORCE = 'Alo' THEN 'PostOnly' ELSE 'RegularTrade' END"""

# Side mapping — HL native A/B → HALO Buy/Sell.
_SIDE_EXPR = """CASE SIDE WHEN 'B' THEN 'Buy' WHEN 'A' THEN 'Sell' END"""

# Price: only populated when this is a limit-like order type.
_PRICE_EXPR = """CASE WHEN TYPE IN ('Limit', 'Stop Limit', 'Take Profit Limit')
            THEN LIMIT_PRICE
        END"""

# StopPx: only populated on trigger orders.
_STOPPX_EXPR = """CASE WHEN IS_TRIGGER THEN TRIGGER_PRICE END"""

# ContingencyType: OCO when the order is a TP/SL bracket (see §5.2).
_CONTINGENCY_EXPR = """CASE WHEN IS_TAKE_PROFIT_OR_STOP_LOSS THEN 'OCO' END"""

# TransactTime: original placement for `open`; status-change time otherwise.
_TRANSACT_TIME_EXPR = """CASE WHEN STATUS = 'open'
            THEN DATE_PART(EPOCH_MILLISECOND, ORDER_TIMESTAMP)::BIGINT
            ELSE DATE_PART(EPOCH_MILLISECOND, STATUS_CHANGE_TIMESTAMP)::BIGINT
        END"""

# OrigTransactTime: original placement, only for non-New rows.
_ORIG_TIME_EXPR = """CASE WHEN STATUS != 'open'
            THEN DATE_PART(EPOCH_MILLISECOND, ORDER_TIMESTAMP)::BIGINT
        END"""

# CumQty = ORIGINAL_SIZE - SIZE. Cast both to numeric to subtract, then back
# to varchar for the CSV. NULL when either operand is non-numeric.
_CUMQTY_EXPR = """TO_VARCHAR(
            TRY_CAST(ORIGINAL_SIZE AS DOUBLE) - TRY_CAST(SIZE AS DOUBLE)
        )"""


_SQL_TEMPLATE = f"""WITH filtered AS (
    SELECT *
    FROM ALLIUM_HYPERLIQUID.RAW.ORDERS
    WHERE ORDER_TIMESTAMP >= %(start_ts)s
      AND ORDER_TIMESTAMP <  %(end_ts)s
      -- Filtered out per hl_orders_halo_mapping.md §8 decisions:
      --   §4.4: `filled` order rows belong to the executions feed.
      --   §5.3: `Vault Close` is not a meaningful TS-algo order.
      AND STATUS != 'filled'
      AND (TYPE IS NULL OR TYPE != 'Vault Close')
      {{extra_filters}}
)
SELECT
    -- HALO Order fields ---------------------------------------------------
    {_TRANSACT_TIME_EXPR}                                              AS TransactTime,
    ORDER_ID                                                           AS Id,
    {_SYMBOL_EXPR}                                                     AS Symbol,
    {_SIDE_EXPR}                                                       AS Side,
    ORIGINAL_SIZE                                                      AS OrderQty,
    {_ORDTYPE_EXPR}                                                    AS OrdType,
    {_PRICE_EXPR}                                                      AS Price,
    {_STATUS_EXPR}                                                     AS Status,
    'Agency'                                                           AS OrderCapacity,
    "USER"                                                             AS Account,
    "USER"                                                             AS ClientId,
    "USER"                                                             AS OriginationTrader,
    {_STOPPX_EXPR}                                                     AS StopPx,
    {_CUMQTY_EXPR}                                                     AS CumQty,
    SIZE                                                               AS LeavesQty,
    {_TIF_EXPR}                                                        AS TimeInForce,
    {_ORIG_TIME_EXPR}                                                  AS OrigTransactTime,
    {_CONTINGENCY_EXPR}                                                AS ContingencyType,
    'Hyperliquid'                                                      AS ExVenue,
    {_TRDTYPE_EXPR}                                                    AS TrdType,
    'hyperliquid'                                                      AS Blockchain,
    "USER"                                                             AS WalletAddress,
    CASE WHEN {_IS_SPOT_EXPR} THEN 'SPOT' ELSE 'SWAP' END               AS SecurityType,
    'Hyperliquid:' || COIN                                             AS ExchangeSymbol,
    CASE WHEN {_IS_SPOT_EXPR} THEN NULL ELSE '1' END                    AS ContractMultiplier,

    -- Supplementary (aux) fields ------------------------------------------
    UNIQUE_ID                                                          AS _UniqueId,
    STATUS                                                             AS _RawStatus,
    SIDE                                                               AS _RawSide,
    TYPE                                                               AS _RawType,
    TIME_IN_FORCE                                                      AS _RawTif,
    COIN                                                               AS _Coin,
    CLIENT_ORDER_ID                                                    AS _ClientOrderId,
    IS_TRIGGER                                                         AS _IsTrigger,
    IS_TAKE_PROFIT_OR_STOP_LOSS                                        AS _IsTpSl,
    IS_REDUCE_ONLY                                                     AS _IsReduceOnly,
    TRIGGER_CONDITION                                                  AS _TriggerCondition,
    TRIGGER_PRICE                                                      AS _TriggerPrice,
    CHILDREN                                                           AS _Children,
    BUILDER_ADDRESS                                                    AS _BuilderAddress,
    BUILDER_FEE                                                        AS _BuilderFee,
    DATE_PART(EPOCH_MILLISECOND, ORDER_TIMESTAMP)::BIGINT              AS _OrderTimestamp,
    DATE_PART(EPOCH_MILLISECOND, STATUS_CHANGE_TIMESTAMP)::BIGINT      AS _StatusChangeTimestamp
FROM filtered
ORDER BY TransactTime, Id
"""


LIST_ORDER_COINS_SQL = """
SELECT
    COIN,
    CASE WHEN COIN LIKE '@%%' OR COIN LIKE '%%/%%' THEN 'spot' ELSE 'perpetuals' END
        AS INFERRED_MARKET_TYPE,
    COUNT(*) AS EVENT_COUNT,
    COUNT(DISTINCT ORDER_ID) AS UNIQUE_ORDERS,
    COUNT(DISTINCT "USER") AS UNIQUE_USERS
FROM ALLIUM_HYPERLIQUID.RAW.ORDERS
WHERE ORDER_TIMESTAMP >= %(start_ts)s
  AND ORDER_TIMESTAMP <  %(end_ts)s
GROUP BY COIN, INFERRED_MARKET_TYPE
ORDER BY EVENT_COUNT DESC
"""
"""Summary query for ``list-order-coins``: distinct COINs in a date range,
with inferred market type and per-COIN activity counts. Useful for picking
a ``--coin`` filter before running a full export.
"""
