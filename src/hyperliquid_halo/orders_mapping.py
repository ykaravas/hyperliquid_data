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
* HIP-3 ``{dex}:{market}`` identifiers emit ``{market}-{DEX}/{quote}``
  (``xyz:TSLA`` → ``TSLA-XYZ/USDC``), the same symbology the executions
  feed and the production pipeline use, so orders and executions of one
  contract share a HALO ``Symbol`` and two builder dexes never collide.
  The quote token is the dex's collateral (:data:`HIP3_DEX_QUOTE_TOKEN`).
* ``@N`` spot pair identifiers are resolved to ``{TOKEN_A}/{TOKEN_B}``
  (``@107`` → ``HYPE/USDC``) through a lookback join on ``DEX.TRADES``,
  the same strings the executions feed emits, so both feeds share one
  ``Symbol`` and ``ExchangeSymbol`` per spot pair. A pair with no trade in
  the lookback keeps the ``{COIN}/USDC`` placeholder.
* ``Blockchain`` is ``ethereum`` (HALO's enum has no Hyperliquid value)
  and ``SymbolType`` comes from the same seeded per-market map as the
  executions feed, so both feeds describe an instrument identically.
* ``Id`` is ``ORDER_ID`` plus the side suffix production puts on execution
  ``OrderID`` (``-B`` for buys, ``-S`` for sells), so an execution shipped
  under ``--halo-strict`` links to its order inside HALO (2026-09-28).
* ``#N`` HIP-4 outcome-market rows are filtered out (out of scope).
* Rows are ordered by ``TransactTime``, ``Id`` and lifecycle rank (New,
  Replaced, then Canceled/Rejected): timestamps are block times, so about
  7% of rows share both keys with another row of the same order.
* ``Text`` carries the raw Hyperliquid status (``badAloPxRejected``,
  ``siblingFilledCanceled``, ...) so the reason the ``Status`` collapse
  discards is visible inside HALO, not only in aux (2026-09-28).
* ``CumQty`` is ``ORIGINAL_SIZE - SIZE`` in exact ``NUMBER(38,12)``
  arithmetic; DOUBLE produced 18-decimal artifacts that breach HALO's
  12-decimal cap (2026-09-28).
* ``Price`` is emitted only for ``Limit`` / ``LimitToStop`` rows and left
  NULL for ``Market`` / ``StopLoss`` on Solidus's instruction (2026-09-28).
* ``Notional`` is ``ORIGINAL_SIZE * LIMIT_PRICE`` on every row: all four
  Hyperliquid quote tokens are dollar-pegged, and without it HALO resolves
  the quote token against its market data and refuses USDE-quoted
  instruments (2026-09-29).
* ``ContingencyType = OCO`` comes from sibling pairing (same user, coin,
  side and ``ORDER_TIMESTAMP``, one Stop-type and one Take-Profit-type
  trigger order), not from ``IS_TAKE_PROFIT_OR_STOP_LOSS``, which is HL's
  position-sized flag (``isPositionTpsl``) (2026-09-28).
* A fired trigger order is re-stamped: its rows after ``triggered`` carry
  ``ORDER_TIMESTAMP`` = trigger time, ``IS_TRIGGER`` false and
  ``TRIGGER_PRICE`` 0, and land in the trigger day's window without the
  armed rows. The ``armed_orders`` lookback CTE (``trigger_lookback_days``)
  supplies the trigger price (``StopPx``), the placement time
  (``OrigTransactTime``, OCO pairing) for those rows; a trigger-type row
  with no armed row inside the lookback degrades to plain Market / Limit so
  the file stays schema-valid (2026-09-28).
* Full-position TP/SL orders arrive with ``ORIGINAL_SIZE = 0``. HALO
  rejects a zero quantity, so the ``position_sizes`` lookback CTE sizes
  them from the trader's position at placement (Allium's per-side start
  position on the latest earlier fill in that market, plus or minus that
  fill), which covers 99.5% of them; the rest are withheld from the HALO
  file by default and counted (2026-09-29).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from .symbol_type_map import render_values_rows

HALO_ORDER_COLUMNS: tuple[str, ...] = (
    "TransactTime",
    "Id",
    "Symbol",
    "Side",
    "OrderQty",
    "OrdType",
    "Price",
    "Notional",
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
    "SymbolType",
    "Text",
)
"""HALO v2.1 order columns emitted in halo_orders.csv, in output order.

Always-NULL fields (``ExpireDateTime``, ``BUIdentifier``, ``IpAddress``,
``ParentOrderId``) are intentionally omitted: HL has no source data for
them, and the HALO uploader can otherwise parse empty columns as literal
``null`` strings. ``Notional`` was added on 2026-09-29 (§5.10).
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
    "_PositionSize",
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
            ``base/quote`` → spot; everything else → perpetuals. ``#N``
            outcome markets never reach this filter; the query drops them
            at source.
        user: Optional on-chain address filter (matches the ``USER``
            column). Useful for per-account analysis.
        spot_lookback_days: How far before ``start_ts`` the ``@N`` spot-pair
            lookup scans ``DEX.TRADES`` for a resolving trade. The
            ``@N`` → tokens mapping is fixed per index, so any trade in the
            window resolves the pair; a pair that did not trade in
            ``[start_ts - lookback, end_ts)`` keeps the ``@N/USDC``
            placeholder. Default 30.
        trigger_lookback_days: How far before ``start_ts`` the
            ``armed_orders`` CTE scans ``RAW.ORDERS`` for the armed rows of
            trigger orders. A fired trigger order is re-stamped to its
            trigger time, so its post-trigger rows fall in the trigger day's
            window while the armed rows (trigger price, placement time, OCO
            pairing keys) stay on the placement day. An order armed before
            the lookback degrades to plain Market / Limit. Default 14.
        position_lookback_days: How far before ``start_ts`` the
            ``position_sizes`` CTE scans ``DEX.TRADES`` for the latest fill
            that gives a trader's position in a market, used as the size of
            full-position TP/SL orders (``ORIGINAL_SIZE = 0``). On
            2026-03-02, 99.8% of such orders had a fill within 30 days and
            the median gap was under an hour. Default 30.
        exclude_post_only: Drop ``TIME_IN_FORCE = 'Alo'`` rows, the post-only
            quoting traffic. On 2026-03-02 those were 1,515,357,584 of the
            1,546,618,738 exportable events (98%), almost all from about
            100 market-making accounts; without them the day is 31,261,154
            rows. Armed trigger rows (NULL TIF) and every Gtc, Ioc and market
            order are kept. Default ``False`` (everything).
    """

    start_ts: datetime
    end_ts: datetime
    coin: str | None = None
    market_type: str | None = None
    user: str | None = None
    spot_lookback_days: int = 30
    trigger_lookback_days: int = 14
    position_lookback_days: int = 30
    exclude_post_only: bool = False

    def __post_init__(self) -> None:
        if self.market_type is not None and self.market_type not in ("spot", "perpetuals"):
            raise ValueError(
                f"market_type must be 'spot' or 'perpetuals', got {self.market_type!r}"
            )
        if self.end_ts <= self.start_ts:
            raise ValueError(
                f"end_ts ({self.end_ts}) must be strictly greater than start_ts ({self.start_ts})"
            )
        if self.spot_lookback_days < 0:
            raise ValueError(
                f"spot_lookback_days must be >= 0, got {self.spot_lookback_days}"
            )
        if self.trigger_lookback_days < 0:
            raise ValueError(
                f"trigger_lookback_days must be >= 0, got {self.trigger_lookback_days}"
            )
        if self.position_lookback_days < 0:
            raise ValueError(
                f"position_lookback_days must be >= 0, got {self.position_lookback_days}"
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
        "spot_lookup_start": _as_utc(params.start_ts) - timedelta(days=params.spot_lookback_days),
        "trigger_lookup_start": (
            _as_utc(params.start_ts) - timedelta(days=params.trigger_lookback_days)
        ),
        "position_lookup_start": (
            _as_utc(params.start_ts) - timedelta(days=params.position_lookback_days)
        ),
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
    if params.exclude_post_only:
        # Post-only quoting (Alo) is 98% of a day's events; armed trigger
        # rows carry a NULL TIF and must stay. See hl_orders_halo_mapping.md §4.3.
        filters.append("AND (TIME_IN_FORCE IS NULL OR TIME_IN_FORCE <> 'Alo')")

    extra_filters = "\n          ".join(filters)
    sql = _SQL_TEMPLATE.format(
        symbol_type_values=render_values_rows(),
        extra_filters=extra_filters,
    )
    return sql, binds


HIP3_DEX_QUOTE_TOKEN: dict[str, str] = {
    "xyz": "USDC",
    "io": "USDC",
    "para": "USDC",
    "mkts": "USDC",
    "abcd": "USDC",
    "hyna": "USDE",
    "km": "USDH",
    "flx": "USDH",
    "vntl": "USDH",
    "cash": "USDT0",
}
"""Collateral (quote) token per HIP-3 perp dex, keyed by the lowercase dex
prefix of ``COIN``. ``RAW.ORDERS`` carries no token symbols, so the quote side
of a HIP-3 ``Symbol`` has to come from here; the executions feed gets it from
Allium's ``TOKEN_B_SYMBOL`` and this map must agree with it so both feeds
emit one ``Symbol`` per contract. Verified against ``DEX.TRADES`` for xyz,
io, para, mkts (USDC) and hyna (USDE) on 2026-09-21; km/flx/vntl (USDH),
cash (USDT0) and abcd (USDC) come from each dex's ``meta.collateralToken``
in the 2026-08-23 universe snapshot. A dex missing from this map falls back
to USDC; add new dexes here when they list.
"""

_HIP3_QUOTE_EXPR = "CASE LOWER(SPLIT_PART(COIN, ':', 1))\n" + "".join(
    f"            WHEN '{dex}' THEN '{quote}'\n" for dex, quote in HIP3_DEX_QUOTE_TOKEN.items()
) + "            ELSE 'USDC'\n        END"

# Symbol derivation: see §3 of hl_orders_halo_mapping.md.
# Spot pairs already containing `/` (PURR/USDC) → pass through.
# @N spot indices → `{TOKEN_A}/{TOKEN_B}` resolved from DEX.TRADES via the
#   spot_pairs CTE (same strings as the executions feed); `{COIN}/USDC`
#   placeholder when the pair did not trade in the lookback window.
# HIP-3 `{dex}:{market}` (xyz:TSLA) → `{market}-{DEX}/{quote}` (TSLA-XYZ/USDC),
#   matching the executions feed / production `TOKEN_A-DEX/TOKEN_B` form.
# Plain perps (BTC), k-prefix (kPEPE) → `{COIN}/USDC`.
# Note: literal `%` is doubled to `%%` so the Snowflake connector's pyformat
# preprocessor passes it through (single `%` triggers placeholder substitution).
_SYMBOL_EXPR = f"""CASE
            WHEN COIN LIKE '%%/%%' THEN COIN
            WHEN COIN LIKE '@%%' THEN COALESCE(sp.token_a || '/' || sp.token_b, COIN || '/USDC')
            WHEN COIN LIKE '%%:%%'
                THEN SPLIT_PART(COIN, ':', 2) || '-' || UPPER(SPLIT_PART(COIN, ':', 1)) || '/' ||
                     {_HIP3_QUOTE_EXPR}
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
# A trigger-type row whose order has no armed row inside the trigger
# lookback (so no trigger price is known, ao.trigger_px IS NULL) degrades to
# the plain Market / Limit it is executing as, because HALO requires StopPx
# on StopLoss / LimitToStop. The raw TYPE stays in aux _RawType. (§5.9)
_ORDTYPE_EXPR = """CASE TYPE
            WHEN 'Limit' THEN 'Limit'
            WHEN 'Market' THEN 'Market'
            WHEN 'Stop Market' THEN IFF(ao.trigger_px IS NULL, 'Market', 'StopLoss')
            WHEN 'Take Profit Market' THEN IFF(ao.trigger_px IS NULL, 'Market', 'StopLoss')
            WHEN 'Stop Limit' THEN IFF(ao.trigger_px IS NULL, 'Limit', 'LimitToStop')
            WHEN 'Take Profit Limit' THEN IFF(ao.trigger_px IS NULL, 'Limit', 'LimitToStop')
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

# Id: the Hyperliquid oid plus the side suffix production appends to execution
# OrderID (BUYER_ORDER_ID || '-B', SELLER_ORDER_ID || '-S'). HALO links an
# execution to its order through OrderID, so the orders feed must carry the
# same suffix; each order has one side, so it is deterministic. The raw oid
# is Id without its last two characters. Decided 2026-09-28 (§6, §8.1 #11).
_ID_EXPR = """ORDER_ID || CASE SIDE WHEN 'B' THEN '-B' WHEN 'A' THEN '-S' END"""



def _lifecycle_rank(qualifier: str) -> str:
    """Render the lifecycle rank used to order rows that share Id and TransactTime (§2).

    ``STATUS_CHANGE_TIMESTAMP`` is a block time (about 13 distinct values per
    second), so an order placed and canceled in one block, or triggered and
    rejected in one block, produces two rows at the same millisecond. New
    comes first, then Replaced (``triggered``), then the terminal row.

    Args:
        qualifier: Table alias to prefix ``STATUS`` with. It must be
            qualified: a bare ``STATUS`` in ``ORDER BY`` resolves to the
            output alias ``Status`` (the HALO value, never ``'open'``), which
            silently disables the rank.

    Returns:
        A SQL ``CASE`` expression evaluating to 0, 1 or 2.
    """
    return f"""CASE WHEN {qualifier}.STATUS = 'open' THEN 0
            WHEN {qualifier}.STATUS = 'triggered' THEN 1
            ELSE 2
        END"""


_LIFECYCLE_RANK_EXPR = _lifecycle_rank("o")
"""Lifecycle rank over the main query's base alias ``o`` (the ``armed`` CTE)."""

# Price: only populated when this is a limit-like order type. NULL for
# Market and StopLoss rows on Solidus's instruction (confirmed 2026-09-28),
# even though LIMIT_PRICE is populated at source on every row (Hyperliquid
# market orders carry their slippage cap as a limit price).
_PRICE_EXPR = """CASE WHEN TYPE IN ('Limit', 'Stop Limit', 'Take Profit Limit')
            THEN LIMIT_PRICE
        END"""

# StopPx: the trigger price from the order's last armed row (armed_orders
# CTE, joined as ao). After `triggered`, the source row has IS_TRIGGER false
# and TRIGGER_PRICE 0 while TYPE stays 'Stop Market' etc., so a plain
# IFF(IS_TRIGGER, ...) left StopLoss / LimitToStop rows without the StopPx
# HALO requires. Decided 2026-09-28 (§5.9, §8.1 #17).
_STOPPX_EXPR = """CASE WHEN TYPE IN ('Stop Market', 'Stop Limit',
                           'Take Profit Market', 'Take Profit Limit')
            THEN ao.trigger_px
        END"""

# ContingencyType: OCO when the order is one leg of a TP/SL pair (§5.2). The
# pair comes from the tpsl_pairs CTE (sibling pairing); the row itself must be
# a trigger-type order so a plain limit placed in the same block by the same
# user does not inherit the mark. IS_TAKE_PROFIT_OR_STOP_LOSS is not used: it
# is HL's position-sized flag (isPositionTpsl), which misses 53% of proven
# pairs and marks standalone stops. Decided 2026-09-28 (§8.1 #18).
_CONTINGENCY_EXPR = """CASE WHEN tp.pair_user IS NOT NULL
             AND TYPE IN ('Stop Market', 'Stop Limit', 'Take Profit Market', 'Take Profit Limit')
            THEN 'OCO'
        END"""

# SymbolType: spot is crypto-only on Hyperliquid, so spot rows are always
# 'Crypto'. Perps take the seeded per-market value keyed on COIN (BTC,
# xyz:TSLA) and nothing else: an unmapped perp emits NULL, never a guess.
# Same policy as the executions feed and production.
_SYMBOL_TYPE_EXPR = f"""CASE WHEN {_IS_SPOT_EXPR} THEN 'Crypto' ELSE m.symbol_type END"""

# TransactTime: original placement for `open`; status-change time otherwise.
_TRANSACT_TIME_EXPR = """CASE WHEN STATUS = 'open'
            THEN DATE_PART(EPOCH_MILLISECOND, ORDER_TIMESTAMP)::BIGINT
            ELSE DATE_PART(EPOCH_MILLISECOND, STATUS_CHANGE_TIMESTAMP)::BIGINT
        END"""

# OrigTransactTime: original placement, only for non-New rows. A fired
# trigger order's post-trigger rows carry ORDER_TIMESTAMP = trigger time, so
# the placement time comes from the armed rows (ao.placed_ts) when known.
_ORIG_TIME_EXPR = """CASE WHEN STATUS != 'open'
            THEN DATE_PART(EPOCH_MILLISECOND, COALESCE(ao.placed_ts, ORDER_TIMESTAMP))::BIGINT
        END"""



def _trim_decimal(expr: str) -> str:
    """Render an exact ``NUMBER(38,12)`` SQL expression as a plain decimal string.

    ``TO_VARCHAR`` renders the fixed scale (``0.061000000000``); the trailing
    zeros and a bare trailing ``.`` are trimmed. ``[.]`` avoids a backslash
    escape inside the SQL literal. NULL stays NULL.

    Args:
        expr: A SQL expression of type ``NUMBER(38,12)``.

    Returns:
        A SQL expression evaluating to the trimmed string.
    """
    return f"REGEXP_REPLACE(TO_VARCHAR({expr}), '[.]?0+$', '')"


_SIZE_NUMERIC = "TRY_CAST(ORIGINAL_SIZE AS NUMBER(38, 12))"
_IS_ZERO_SIZE = f"{_SIZE_NUMERIC} = 0"

# Quantity of the order in exact decimal: ORIGINAL_SIZE, except that a
# full-position TP/SL order (ORIGINAL_SIZE = 0, §5.8) takes the trader's
# position at placement from the position_sizes CTE (ps). Post-trigger rows
# of such an order carry a real ORIGINAL_SIZE at the source and keep it.
# Stays 0 when the position could not be resolved (the exporter withholds
# those rows by default).
_QTY_NUMERIC = f"""COALESCE(IFF({_IS_ZERO_SIZE}, ps.position_size, NULL), {_SIZE_NUMERIC})"""

# OrderQty / LeavesQty: the raw strings, except for sized full-position rows.
_ORDER_QTY_EXPR = f"""CASE WHEN {_IS_ZERO_SIZE} AND ps.position_size IS NOT NULL
            THEN {_trim_decimal("ps.position_size")}
            ELSE ORIGINAL_SIZE
        END"""
_LEAVES_QTY_EXPR = f"""CASE WHEN {_IS_ZERO_SIZE} AND ps.position_size IS NOT NULL
            THEN {_trim_decimal("ps.position_size")}
            ELSE SIZE
        END"""

# Aux: the position-derived size when a row was sized that way, else NULL.
_POSITION_SIZE_EXPR = f"""IFF({_IS_ZERO_SIZE}, {_trim_decimal("ps.position_size")}, NULL)"""

# CumQty = ORIGINAL_SIZE - SIZE in exact decimal. DOUBLE arithmetic produced
# binary artifacts such as 0.060999999999999999 (18 decimals), breaching
# HALO's 12-decimal cap. NULL when either operand is non-numeric.
_CUMQTY_EXPR = _trim_decimal(
    "TRY_CAST(ORIGINAL_SIZE AS NUMBER(38, 12)) - TRY_CAST(SIZE AS NUMBER(38, 12))"
)

# Notional: USD value of the order, OrderQty * LIMIT_PRICE in exact decimal
# (the product's scale is 12). Every Hyperliquid quote token (USDC, USDH,
# USDT0, USDE) is dollar-pegged, so quote units are USD to within the peg.
# Sent on every row, including the Market / StopLoss rows whose Price is
# withheld, because HALO otherwise resolves the quote token against its
# market data and refuses instruments quoted in USDE (§5.10, 2026-09-29).
# NULL when either input is non-numeric.
_NOTIONAL_EXPR = _trim_decimal(f"{_QTY_NUMERIC} * TRY_CAST(LIMIT_PRICE AS NUMBER(38, 12))")


_SQL_TEMPLATE = f"""WITH symbol_type_map AS (
    SELECT map_coin, symbol_type FROM VALUES
        {{symbol_type_values}}
    AS t(map_coin, symbol_type)
),
-- @N spot pair index -> token symbols, taken from Allium's own enrichment of
-- DEX.TRADES so the strings equal what the executions feed emits. The mapping
-- is fixed per index (a spot index never changes pair), so any trade in the
-- lookback window resolves it; ANY_VALUE is safe.
spot_pairs AS (
    SELECT
        COIN                          AS spot_coin,
        ANY_VALUE(TOKEN_A_SYMBOL)     AS token_a,
        ANY_VALUE(TOKEN_B_SYMBOL)     AS token_b,
        ANY_VALUE(NULLIF(PAIR, ''))   AS pair
    FROM ALLIUM_HYPERLIQUID.DEX.TRADES
    WHERE MARKET_TYPE = 'spot'
      AND COIN LIKE '@%%'
      AND TOKEN_A_SYMBOL IS NOT NULL
      AND TOKEN_B_SYMBOL IS NOT NULL
      AND TIMESTAMP >= %(spot_lookup_start)s
      AND TIMESTAMP <  %(end_ts)s
    GROUP BY COIN
),
filtered AS (
    SELECT *
    FROM ALLIUM_HYPERLIQUID.RAW.ORDERS
    WHERE ORDER_TIMESTAMP >= %(start_ts)s
      AND ORDER_TIMESTAMP <  %(end_ts)s
      -- Filtered out per hl_orders_halo_mapping.md §8 decisions:
      --   §4.4: `filled` order rows belong to the executions feed.
      --   §5.3: `Vault Close` is not a meaningful TS-algo order.
      --   §5.7: `#N` HIP-4 outcome markets are out of scope (2026-09-28).
      AND STATUS != 'filled'
      AND (TYPE IS NULL OR TYPE != 'Vault Close')
      AND COIN NOT LIKE '#%%'
      {{extra_filters}}
),
-- Every trigger order armed in [start - trigger_lookback_days, end): its
-- trigger price, placement time and pairing keys. Needed because a fired
-- trigger order is re-stamped: its rows after `triggered` carry
-- ORDER_TIMESTAMP = trigger time, IS_TRIGGER false and TRIGGER_PRICE 0, and
-- land in the trigger day's window without their armed rows (§5.9). Armed
-- rows keep the placement ORDER_TIMESTAMP, so MIN() is the placement time.
armed_orders AS (
    SELECT
        ORDER_ID                                       AS armed_order_id,
        MAX_BY(TRIGGER_PRICE, STATUS_CHANGE_TIMESTAMP) AS trigger_px,
        MIN(ORDER_TIMESTAMP)                           AS placed_ts,
        ANY_VALUE("USER")                              AS armed_user,
        ANY_VALUE(COIN)                                AS armed_coin,
        ANY_VALUE(SIDE)                                AS armed_side,
        ANY_VALUE(TYPE)                                AS armed_type
    FROM ALLIUM_HYPERLIQUID.RAW.ORDERS
    WHERE IS_TRIGGER
      AND ORDER_TIMESTAMP >= %(trigger_lookup_start)s
      AND ORDER_TIMESTAMP <  %(end_ts)s
      AND COIN NOT LIKE '#%%'
    GROUP BY ORDER_ID
),
-- TP/SL legs placed together: one Stop-type and one Take-Profit-type trigger
-- order from the same user on the same coin and side in the same block
-- (placement ORDER_TIMESTAMP). Position TP/SL pairs and order-attached
-- children both arrive this way; Hyperliquid exposes no pair id. Built from
-- the lookback so a pair placed before the window still pairs.
tpsl_pairs AS (
    SELECT
        armed_user AS pair_user,
        armed_coin AS pair_coin,
        armed_side AS pair_side,
        placed_ts  AS pair_ts
    FROM armed_orders
    GROUP BY 1, 2, 3, 4
    HAVING COUNT_IF(armed_type LIKE 'Stop%%') > 0
       AND COUNT_IF(armed_type LIKE 'Take Profit%%') > 0
),
-- Full-position TP/SL orders arrive with ORIGINAL_SIZE = 0 (§5.8) and HALO
-- rejects a zero quantity. Their size is the trader's position when they were
-- placed: the latest perp fill by that trader in that market at or before
-- ORDER_TIMESTAMP, using Allium's per-side start position (position after
-- the fill = start position + amount for the buyer, - amount for the seller),
-- preferring the tail of that fill's block when several fills share it.
position_keys AS (
    SELECT DISTINCT ORDER_ID, "USER" AS pk_user, COIN AS pk_coin, ORDER_TIMESTAMP AS pk_ts
    FROM filtered
    WHERE {_IS_ZERO_SIZE}
),
position_fills_raw AS (
    SELECT t.BUYER_ADDRESS AS pf_user, t.COIN AS pf_coin, t.TIMESTAMP AS pf_ts,
           t.BUYER_START_POSITION::NUMBER(38, 12)                     AS pf_start,
           t.BUYER_START_POSITION::NUMBER(38, 12)
             + t.AMOUNT::NUMBER(38, 12)                                AS pf_pos
    FROM ALLIUM_HYPERLIQUID.DEX.TRADES t
    JOIN (SELECT DISTINCT pk_user, pk_coin FROM position_keys) k
      ON k.pk_user = t.BUYER_ADDRESS AND k.pk_coin = t.COIN
    WHERE t.MARKET_TYPE = 'perpetuals'
      AND t.TIMESTAMP >= %(position_lookup_start)s
      AND t.TIMESTAMP <  %(end_ts)s
    UNION ALL
    SELECT t.SELLER_ADDRESS, t.COIN, t.TIMESTAMP,
           t.SELLER_START_POSITION::NUMBER(38, 12),
           t.SELLER_START_POSITION::NUMBER(38, 12)
             - t.AMOUNT::NUMBER(38, 12)
    FROM ALLIUM_HYPERLIQUID.DEX.TRADES t
    JOIN (SELECT DISTINCT pk_user, pk_coin FROM position_keys) k
      ON k.pk_user = t.SELLER_ADDRESS AND k.pk_coin = t.COIN
    WHERE t.MARKET_TYPE = 'perpetuals'
      AND t.TIMESTAMP >= %(position_lookup_start)s
      AND t.TIMESTAMP <  %(end_ts)s
),
-- Fills in one block share a TIMESTAMP and Allium gives no order inside the
-- block. The block's tail is the fill whose resulting position is no other
-- fill's start position in that block: the position after the block.
position_fills AS (
    SELECT pf_user, pf_coin, pf_ts, pf_pos,
           NOT ARRAY_CONTAINS(
               pf_pos::VARIANT,
               ARRAY_AGG(pf_start::VARIANT) OVER (PARTITION BY pf_user, pf_coin, pf_ts)
           )                                                          AS pf_tail
    FROM position_fills_raw
),
position_sizes AS (
    SELECT ORDER_ID AS ps_order_id, ABS(pf_pos) AS position_size
    FROM (
        SELECT k.ORDER_ID, f.pf_pos,
               ROW_NUMBER() OVER (
                   PARTITION BY k.ORDER_ID ORDER BY f.pf_ts DESC, f.pf_tail DESC
               ) AS rn
        FROM position_keys k
        JOIN position_fills f
          ON f.pf_user = k.pk_user AND f.pf_coin = k.pk_coin AND f.pf_ts <= k.pk_ts
    )
    WHERE rn = 1 AND pf_pos <> 0
)
SELECT
    -- HALO Order fields ---------------------------------------------------
    {_TRANSACT_TIME_EXPR}                                              AS TransactTime,
    {_ID_EXPR}                                                         AS Id,
    {_SYMBOL_EXPR}                                                     AS Symbol,
    {_SIDE_EXPR}                                                       AS Side,
    {_ORDER_QTY_EXPR}                                                  AS OrderQty,
    {_ORDTYPE_EXPR}                                                    AS OrdType,
    {_PRICE_EXPR}                                                      AS Price,
    {_NOTIONAL_EXPR}                                                   AS Notional,
    {_STATUS_EXPR}                                                     AS Status,
    'Agency'                                                           AS OrderCapacity,
    "USER"                                                             AS Account,
    "USER"                                                             AS ClientId,
    "USER"                                                             AS OriginationTrader,
    {_STOPPX_EXPR}                                                     AS StopPx,
    {_CUMQTY_EXPR}                                                     AS CumQty,
    {_LEAVES_QTY_EXPR}                                                 AS LeavesQty,
    {_TIF_EXPR}                                                        AS TimeInForce,
    {_ORIG_TIME_EXPR}                                                  AS OrigTransactTime,
    {_CONTINGENCY_EXPR}                                                AS ContingencyType,
    'Hyperliquid'                                                      AS ExVenue,
    {_TRDTYPE_EXPR}                                                    AS TrdType,
    -- HALO's Blockchain enum has no 'Hyperliquid' value; 'ethereum' is the
    -- closest valid one (HL accounts are EVM addresses). Same as executions.
    'ethereum'                                                         AS Blockchain,
    "USER"                                                             AS WalletAddress,
    CASE WHEN {_IS_SPOT_EXPR} THEN 'SPOT' ELSE 'SWAP' END               AS SecurityType,
    'Hyperliquid:' || COALESCE(sp.pair, COIN)                          AS ExchangeSymbol,
    CASE WHEN {_IS_SPOT_EXPR} THEN NULL ELSE '1' END                    AS ContractMultiplier,
    {_SYMBOL_TYPE_EXPR}                                                AS SymbolType,
    -- Raw Hyperliquid status: the specific reject / cancel reason the Status
    -- collapse discards, visible inside HALO (aux _RawStatus keeps a copy).
    STATUS                                                             AS Text,

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
    {_POSITION_SIZE_EXPR}                                              AS _PositionSize,
    IS_REDUCE_ONLY                                                     AS _IsReduceOnly,
    TRIGGER_CONDITION                                                  AS _TriggerCondition,
    TRIGGER_PRICE                                                      AS _TriggerPrice,
    CHILDREN                                                           AS _Children,
    BUILDER_ADDRESS                                                    AS _BuilderAddress,
    BUILDER_FEE                                                        AS _BuilderFee,
    DATE_PART(EPOCH_MILLISECOND, ORDER_TIMESTAMP)::BIGINT              AS _OrderTimestamp,
    DATE_PART(EPOCH_MILLISECOND, STATUS_CHANGE_TIMESTAMP)::BIGINT      AS _StatusChangeTimestamp
FROM filtered o
LEFT JOIN armed_orders ao ON ao.armed_order_id = o.ORDER_ID
LEFT JOIN position_sizes ps ON ps.ps_order_id = o.ORDER_ID
LEFT JOIN symbol_type_map m ON m.map_coin = o.COIN
LEFT JOIN spot_pairs sp ON sp.spot_coin = o.COIN
LEFT JOIN tpsl_pairs tp
       ON tp.pair_user = o."USER"
      AND tp.pair_coin = o.COIN
      AND tp.pair_side = o.SIDE
      AND tp.pair_ts   = COALESCE(ao.placed_ts, o.ORDER_TIMESTAMP)
ORDER BY TransactTime, Id, {_LIFECYCLE_RANK_EXPR}
"""


LIST_ORDER_COINS_SQL = """
SELECT
    COIN,
    CASE WHEN COIN LIKE '#%%' THEN 'outcome (excluded)'
         WHEN COIN LIKE '@%%' OR COIN LIKE '%%/%%' THEN 'spot'
         ELSE 'perpetuals' END
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
a ``--coin`` filter before running a full export. ``#N`` HIP-4 outcome
markets are labelled ``outcome (excluded)`` because ``export-orders``
drops them at source.
"""
