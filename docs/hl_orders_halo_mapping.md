# Hyperliquid Orders → HALO v2.1 Mapping

Field-by-field mapping from Allium's `ALLIUM_HYPERLIQUID.RAW.ORDERS` table
into Solidus's HALO Trade Surveillance v2.1 **Order Data** schema, plus
the special cases discovered while sampling the source data.

## 1. Source table at a glance

`ALLIUM_HYPERLIQUID.RAW.ORDERS` — 24 columns, event-sourced (one row per
status change, not one row per order). Distinct from the trades table:

- No `MARKET_TYPE` / `TOKEN_A_SYMBOL` / `TOKEN_B_SYMBOL` enrichment —
  spot vs. perp must be inferred from `COIN` (see §3).
- No counterparty side — orders are unilateral.
- One column (`UNIQUE_ID`) is row-unique; `ORDER_ID` is the actual
  Hyperliquid `oid` and recurs across rows for a given order.
- A sixth `COIN` shape, `#N` (HIP-4 outcome markets), exists in the
  source and is dropped at source. See §5.7.

### 1.1 Data-availability caveats (Allium)

- **Not backfillable.** Orders coverage starts when Allium spun up
  Hyperliquid nodes. Trades pre-dating that window have no order rows.
- **`open` status missing before 2025-06-08 12:18:08 UTC.** Prior to that
  date the node feed did not emit orders at placement time; only later
  status changes (`canceled`, `triggered`, rejects) were recorded.
  Out of scope for this mapping — see §8.

## 2. Grain — one row per status change

Sampled over 1 hour:

| Rows per `ORDER_ID` | Count        |
|---------------------|--------------|
| 1                   | 365 M (76%)  |
| 2                   | 110 M (23%)  |
| 3                   | 13 K (≪1%)   |

A typical lifecycle (`open → canceled`, `open → filled`,
`open → triggered → filled`, or a single terminal `*Rejected`) writes 1–3
rows. Each row carries the full order snapshot at that moment:
`ORIGINAL_SIZE` is constant, `SIZE` is the remaining-at-event quantity,
`STATUS_CHANGE_TIMESTAMP` advances with the event, `ORDER_TIMESTAMP`
stays pinned to the original placement, with one exception: a fired
trigger order is re-stamped, so its rows after `triggered` carry the
trigger time (§5.9).

**HALO consequence.** Each Allium row maps to one HALO order row. All
rows for the same `ORDER_ID` share `Id`. The `New` row uses
`TransactTime = ORDER_TIMESTAMP`; non-`New` rows use
`TransactTime = STATUS_CHANGE_TIMESTAMP` and
`OrigTransactTime = ORDER_TIMESTAMP`.

`filled` status rows are **routed to the executions output**, not the
orders output — HALO order `Status` excludes `Filled`/`PartiallyFilled`.

**Row order.** `STATUS_CHANGE_TIMESTAMP` is a block time: about 13
distinct values per second, so an order placed and canceled inside one
block, or triggered and rejected inside one block, produces two rows at
the same millisecond. Profiled over 2026-08-20 12:00 to 12:10 UTC, 7% of
rows (609,605 orders, 1.22 M rows of 16.47 M) share `Id` and
`TransactTime` with another row of the same order; 560,301 of those
pairs are `open` + `canceled`. The SQL therefore orders the file by
`TransactTime`, `Id`, then a lifecycle rank: `New` (0), `Replaced` (1),
then the terminal `Canceled` / `Rejected` row (2), so HALO never sees a
terminal state before the state it ends. Decided 2026-09-28 (§8.1 #13).

## 3. `COIN` format — Symbol / SecurityType derivation

`COIN` is the only instrument identifier on the orders table and uses
six distinct formats, one of which is excluded. Distribution from a
2-hour sample (~660 M rows):

| `COIN` shape         | Example       | Share  | Means                                                  |
|----------------------|---------------|--------|--------------------------------------------------------|
| Plain name           | `BTC`, `ETH`  | 96.5 % | Standard HL perpetual, USDC-margined.                  |
| `k`-prefix           | `kPEPE`       | 1.5 %  | HL "1000×" perpetual (`kPEPE` = 1000 PEPE units).      |
| `xyz:*`              | `xyz:SP500`   | (rare) | HIP-3 builder-deployed perp (`{perpDex}:{marketName}`). See §5.4. |
| `@N` (numeric index) | `@107`        | 1.9 %  | HL spot pair, identified by internal index.            |
| `base/quote`         | `PURR/USDC`   | 0.02 % | Legacy spot pair (`PURR/USDC` only at time of sampling).|
| `#N` (numeric index) | `#11300`      | (rare) | HIP-4 outcome (prediction) market: paired yes/no indices, prices between 0 and 1. **Dropped at source**, see §5.7. |

Inferred mapping:

| `COIN` shape | `SecurityType` | `Symbol`          | `ExchangeSymbol`            | `ContractMultiplier` |
|--------------|----------------|-------------------|-----------------------------|----------------------|
| Plain        | `SWAP`         | `{COIN}/USDC`     | `Hyperliquid:{COIN}`        | `'1'`                |
| `k`-prefix   | `SWAP`         | `{COIN}/USDC` (keep `k`) | `Hyperliquid:{COIN}` | `'1'`                |
| `{dex}:*`    | `SWAP`         | `{market}-{DEX}/{quote}` (`xyz:TSLA` → `TSLA-XYZ/USDC`, `hyna:1000PEPE` → `1000PEPE-HYNA/USDE`); quote is the dex's collateral, see §5.4 | `Hyperliquid:{COIN}` | `'1'` |
| `@N`         | `SPOT`         | `{TOKEN_A}/{TOKEN_B}` resolved from `DEX.TRADES` (`@107` → `HYPE/USDC`); `{COIN}/USDC` placeholder only when the pair had no trade in the lookback, see §5.1 | `Hyperliquid:{PAIR}` when resolved (`Hyperliquid:HYPE/USDC`), else `Hyperliquid:{COIN}` | `NULL` |
| `base/quote` | `SPOT`         | `COIN` (already `base/quote`) | `Hyperliquid:{COIN}` | `NULL` |
| `#N`         | (row dropped at source, §5.7) | | | |

> The 41-character HALO `Symbol` cap is comfortable for all observed
> HIP-3 names (`SP500-XYZ/USDC` is 14 characters).

The perp and legacy-spot forms coincide with what the executions feed
derives from `TOKEN_A_SYMBOL` / `TOKEN_B_SYMBOL` (for main-dex perps
`TOKEN_A_SYMBOL = COIN` and `TOKEN_B_SYMBOL = USDC`), so orders and
executions of one instrument share a HALO `Symbol`. Resolved `@N` pairs
also share `ExchangeSymbol` with the executions feed (`Hyperliquid:{PAIR}`).

## 4. Enum translations

### 4.1 `SIDE` (HL native → HALO)

Hyperliquid stores **`A`** (ask = sell) and **`B`** (bid = buy). HALO
wants `Buy` / `Sell`. The trades table already pre-translates this;
the orders table does **not**.

| `SIDE` | HALO `Side` |
|--------|-------------|
| `B`    | `Buy`       |
| `A`    | `Sell`      |

### 4.2 `TYPE` → `OrdType`

Distinct values (2-hour sample):

| `TYPE`               | Share   | HALO `OrdType` | Notes |
|----------------------|---------|----------------|-------|
| `Limit`              | 99.95 % | `Limit`        | |
| `Stop Market`        | 0.024 % | `StopLoss`     | Conditional market; HALO has no separate take-profit enum. |
| `Market`             | 0.022 % | `Market`       | |
| `Take Profit Market` | 0.0066 %| `StopLoss`     | Same enum as Stop Market (direction implicit in trigger). |
| `Stop Limit`         | 0.0039 %| `LimitToStop`  | |
| `Take Profit Limit`  | 0.0024 %| `LimitToStop`  | |

`Vault Close` rows are **filtered out** of the orders feed at source.
See §5.3.

### 4.3 `TIME_IN_FORCE` → `TimeInForce` (+ `TrdType`)

| `TIME_IN_FORCE`     | Share  | HALO `TimeInForce`  | HALO `TrdType` | Notes |
|---------------------|--------|---------------------|----------------|-------|
| `Alo`               | 97.4 % | `GoodTillCancel`    | `PostOnly`     | **ALO = Add Liquidity Only**, Hyperliquid's name for post-only. The order rests passively on the book; if it would immediately cross the spread on placement, it is rejected with `badAloPxRejected` instead of executing as a taker. HALO has no ALO enum, so the post-only intent is carried by `TrdType`. |
| `Ioc`               | 1.9 %  | `ImmediateOrCancel` | `RegularTrade` | |
| `Gtc`               | 0.6 %  | `GoodTillCancel`    | `RegularTrade` | |
| `FrontendMarket`    | 0.022 %| `ImmediateOrCancel` | `RegularTrade` | UI-spawned market order with frontend-bounded slippage. |
| `LiquidationMarket` | 6e-4 % | `ImmediateOrCancel` | `RegularTrade` | Forced liquidation. Surfaced via aux `_RawTif`. |
| `''` (empty)        | 0.034 %| `GoodTillCancel`    | `RegularTrade` | Empty for all `IS_TRIGGER=true` rows (trigger orders have no resting TIF until they fire). |

### 4.4 `STATUS` → `Status`

HALO order `Status` enum: `New`, `Canceled`, `Restate`, `Replaced`,
`Expired`, `Rejected`. `Filled` / `PartiallyFilled` are **execution**
statuses and not valid here.

| HL `STATUS` (23 observed)                                                                                                                                                                                                                                                                       | HALO `Status` | Routing |
|-----------------|---|---|
| `open`                                                                                                                                                                                                                                                                                          | `New`         | order row |
| `triggered`                                                                                                                                                                                                                                                                                     | `Replaced`    | order row — trigger condition fired, conditional order is now a live limit |
| `filled`                                                                                                                                                                                                                                                                                        | n/a           | **filtered out** — fills are already covered by the trades pipeline (`DEX.TRADES` → HALO executions). HALO order `Status` does not accept `Filled` regardless. |
| `canceled`, `reduceOnlyCanceled`, `selfTradeCanceled`, `siblingFilledCanceled`, `marginCanceled`, `scheduledCancel`, `openInterestCapCanceled`, `outcomeSettledCanceled`, `liquidatedCanceled`                                                                                                   | `Canceled`    | order row |
| `badAloPxRejected`, `perpMarginRejected`, `iocCancelRejected`, `insufficientSpotBalanceRejected`, `reduceOnlyRejected`, `minTradeNtlRejected`, `positionIncreaseAtOpenInterestCapRejected`, `positionFlipAtOpenInterestCapRejected`, `tooAggressiveAtOpenInterestCapRejected`                    | `Rejected`    | order row |

The collapsed-away detail (e.g., `badAloPxRejected` vs `iocCancelRejected`)
is carried in HALO `Text` (§6) and in aux `_RawStatus`.

Volume-wise, `badAloPxRejected` dominates (≈59 % of all order events):
post-only orders that would have crossed the spread are rejected
immediately. Surveillance models should weight this accordingly.

**Scale, and the `--exclude-post-only` option.** A full day is far
larger than the executions feed: 2026-03-02 has 1,546,618,738 exportable
order events (the executions day of 2026-04-20 had 8,660,378 rows).
1,515,357,584 of them (98%) are `Alo`, and the 100 busiest accounts
produce 74% of all rows; 829 M are rejects, 361 M `open`, 356 M cancels,
66,685 `triggered`. As CSV that day is about 1 TB and roughly 1,200 HALO
parts. `export-orders --exclude-post-only` (2026-09-29) drops the `Alo`
rows at source (`TIME_IN_FORCE IS NULL OR TIME_IN_FORCE <> 'Alo'`, so the
NULL-TIF armed trigger rows stay) and leaves 31,261,154 rows: 17.1 M
`Ioc`, 13.2 M `Gtc`, 731,524 armed trigger rows, 273,830 `FrontendMarket`.
Whether HALO should receive the quoting traffic at all is a question for
Solidus (§8.2 #12); until then the option is how a whole day gets
exported.

## 5. Special cases / gotchas

### 5.1 Spot symbol resolution

`@N`-format coins (e.g. `@107`) are HL's internal spot pair indices.
They cannot be resolved to a human-readable `base/quote` from the orders
table alone, and Allium ships no spot-pair reference table
(`ALLIUM_HYPERLIQUID` holds only `DEX.TRADES`, `RAW.FILLS`, `RAW.ORDERS`
and the transfer tables).

**Decision (current, changed 2026-09-21):** resolve through Allium's own
enrichment of `DEX.TRADES`. The orders SQL builds a `spot_pairs` CTE,
`SELECT COIN, ANY_VALUE(TOKEN_A_SYMBOL), ANY_VALUE(TOKEN_B_SYMBOL),
ANY_VALUE(PAIR) FROM DEX.TRADES WHERE MARKET_TYPE = 'spot' AND COIN LIKE
'@%' AND both token symbols are non-NULL AND TIMESTAMP in
[start - lookback, end)`, and LEFT JOINs it on `COIN`. A resolved pair
emits `Symbol = TOKEN_A/TOKEN_B` (`@107` → `HYPE/USDC`) and
`ExchangeSymbol = Hyperliquid:{PAIR}`, exactly the strings the executions
feed emits for the same pair. `ANY_VALUE` is safe because a spot index
never changes pair (298 spot coins traded since 2026-06-01, none with
more than one `PAIR`).

The lookback defaults to 30 days (`--spot-lookback-days`,
`OrdersQueryParams.spot_lookback_days`). A pair that had orders but no
trade in `[start - lookback, end)`, or that Allium itself has not named
yet (new listings show `TOKEN_A_SYMBOL` NULL for a while), keeps the
`{COIN}/USDC` placeholder (`@707/USDC`), and the exporter reports the
affected pairs and row counts (`OrdersExportResult.unresolved_spot_coins`,
a CLI warning). Widen the lookback to resolve illiquid pairs. The raw
`@N` is always in aux `_Coin`.

The alternative, a snapshot of Hyperliquid's `/info → spotMeta` index
→ token map, was not chosen: it would introduce a second hand-refreshed
map and its token names are not guaranteed to equal Allium's
`TOKEN_A_SYMBOL` strings, which is what the executions feed uses.

### 5.2 TP/SL pairs (OCO)

Hyperliquid places take-profit and stop-loss orders in two ways. A
**position TP/SL** (`grouping = positionTpsl`, `isPositionTpsl = true`,
Allium's `IS_TAKE_PROFIT_OR_STOP_LOSS`) is sized to the position and
placed directly. An **order-attached TP/SL** (`grouping = normalTpsl`)
rides along inside the parent order's `CHILDREN` JSON until the parent
fills, and is then placed as a trigger order of its own with a new
`oid` and no back-reference to the parent. In both cases a TP and an SL
placed together cancel each other when one fills
(`siblingFilledCanceled`), which is HALO's `OCO`. Hyperliquid exposes no
pair id.

`IS_TAKE_PROFIT_OR_STOP_LOSS` is therefore **not** an OCO marker, and
the data says so: over 2026-08-20 12:00 to 12:10 UTC, 127 of the 240
`siblingFilledCanceled` rows (53%) had the flag false, while only 224 of
the 1,312 armed trigger orders opened with the flag true (17%) had a
complementary sibling. The flag stayed the OCO source in this mapping
until 2026-09-28; the raw value is still in aux `_IsTpSl`.

**Decision (2026-09-28):** derive `OCO` by **sibling pairing**. The
`tpsl_pairs` CTE groups the `open` rows of armed trigger orders by
`USER`, `COIN`, `SIDE` and `ORDER_TIMESTAMP` (both legs are placed in
the same block) and keeps the groups that hold at least one Stop-type
and one Take-Profit-type order. Every lifecycle row of a trigger-type
order whose keys match such a group gets `ContingencyType = OCO`; a plain
limit order the same user placed in the same block does not, because
the mark is restricted to `TYPE IN ('Stop Market', 'Stop Limit',
'Take Profit Market', 'Take Profit Limit')`. The rule found the sibling
for 186 of the 206 sibling-canceled orders placed that day (90%),
including 98 of the 108 the flag missed. Orders whose `open` row is
missing (before 2025-06-08, §1.1) cannot be paired.

Brackets are kept **inline**: one HALO row per source row, no synthetic
per-leg ids, `ParentOrderId` left empty (there is nothing to point it
at). The raw `CHILDREN` JSON stays in aux `_Children`.

### 5.3 `Vault Close`

A non-user-initiated order type emitted when a vault is unwound. Rare
(~160 occurrences per 2 hours).

**Decision (current):** filter `TYPE = 'Vault Close'` out of the orders
feed entirely. Vaults are not in surveillance scope today, and a forced
unwind isn't a meaningful "order" from a TS-algo perspective. Revisit
when vault activity becomes in scope — see §8.

### 5.4 HIP-3 `xyz:*` symbols

HIP-3 perp markets are builder-deployed and use the convention
`{perpDex}:{marketName}` in `COIN` (e.g. `xyz:SP500`,
`xyz:XYZ100`, `xyz:SILVER`).

**Decision (current, changed 2026-09-21):** emit the production
symbology `{market}-{DEX}/{quote}`: `SPLIT_PART(COIN, ':', 2) || '-' ||
UPPER(SPLIT_PART(COIN, ':', 1)) || '/' || quote`, so `xyz:SP500` becomes
`SP500-XYZ/USDC`. This is what the executions feed derives from Allium's
`TOKEN_A_SYMBOL` / `PERP_DEX` / `TOKEN_B_SYMBOL` and what the production
pipeline ships (its DQ-8 check requires HIP-3 symbols to match
`-[A-Z]+/`). Before this change the orders feed emitted `xyz:SP500/USDC`
while the executions feed emitted `SP500/USDC`, so HALO saw two symbols
for one contract and could not link its orders to its executions.

**Why keep the dex at all:** HIP-3 lets independent builders deploy their
own perp dexes, and market names are **not** globally unique: `xyz:TSLA`
and `cash:TSLA` are different contracts with different collateral.
Collapsing them into one `Symbol` would make surveillance treat them as
a single venue.

**Quote token.** `RAW.ORDERS` carries no token symbols, so the quote side
comes from `orders_mapping.HIP3_DEX_QUOTE_TOKEN`, keyed by the lowercase
dex prefix: xyz, io, para, mkts, abcd → `USDC`; hyna → `USDE`; km, flx,
vntl → `USDH`; cash → `USDT0`; anything else → `USDC`. The USDC and USDE
entries were verified against `DEX.TRADES.TOKEN_B_SYMBOL` on 2026-09-21;
the USDH / USDT0 entries come from each dex's `meta.collateralToken` in
the 2026-08-23 universe snapshot. Add new dexes there when they list so
the two feeds keep agreeing (§8.2).

### 5.5 `BUILDER_ADDRESS` / `BUILDER_FEE`

Set on ~0.3 % of rows — orders routed via a builder code (the HL
analogue of an introducing broker). Carried through to aux only; HALO
has no native field for builder attribution.

### 5.6 `CLIENT_ORDER_ID`

HL exposes a user-supplied 16-byte hex client-order-id (`cloid`). Set on
~93 % of rows. Carried in aux `_ClientOrderId`; **not** mapped to HALO
`ClientId` (which is the beneficiary identifier, not the client tag).

### 5.7 HIP-4 outcome markets (`#N`)

`COIN` values of the form `#N` (`#11290`, `#11291`, `#11300`, `#11301`,
...) are Hyperliquid outcome (prediction) markets: they come in
consecutive yes/no pairs, trade between 0 and 1, reject with
`insufficientSpotBalanceRejected`, and Hyperliquid has an
`outcomeSettledCanceled` status for them. August 2026 holds 609 such
coins and 40.0 M order rows. They have no rows in `DEX.TRADES` (so the
executions feed never sees their fills either) but do appear in
`RAW.FILLS`.

**Decision (2026-09-28):** out of scope. The orders SQL drops them at
source (`COIN NOT LIKE '#%'`), `list-order-coins` labels them
`outcome (excluded)`, and no HALO row is emitted. Before this change the
shape fell through to the perp branch and would have shipped as
`#11300/USDC`, `SWAP`, `PerpetualFutures`, wrong on every count.
Bringing them in scope needs a name source (Allium's enrichment does not
cover them), a `SecurityType` decision and `SymbolType = EventContracts`.
See §8.2 #7.

### 5.8 Full-position TP/SL orders (`ORIGINAL_SIZE = 0`)

A position TP/SL "defaults to the entire position size" (Hyperliquid
docs), and Allium encodes that as `ORIGINAL_SIZE = '0'` until the order
triggers. Over 2026-08-20 12:00 to 12:10 UTC that was 1,801 of 6,171
Stop Market rows (29%), 1,344 of 3,012 Take Profit Market rows (45%),
51 of 383 Take Profit Limit rows and 9 of 681 Stop Limit rows, all with
`IS_TAKE_PROFIT_OR_STOP_LOSS = true`; 716 of them were `triggered` or
`siblingFilledCanceled` rows, the stop-hunt events a surveillance feed
exists for.

HALO's schema says `OrderQty` "must be > 0 except for `Reverse`", but its
ClickHouse copy applies no quantity filter to orders, so whether the
upload validator rejects a zero is unknown until tried. Two feeds also
meet here: when such a stop fires, its execution reaches HALO with the
real quantity and links to this order, so `CumQty` above `OrderQty`
becomes possible.

**Decision (2026-09-28):** keep the rows and ship `OrderQty = 0` by
default; never drop them silently. The exporter counts them
(`OrdersExportResult.zero_qty_rows`, a warning and a CLI note), and
`--drop-zero-qty` withholds them from `halo_orders.csv` while keeping
them in `aux_orders.csv`, should HALO reject a zero quantity.

**Answered 2026-09-29:** HALO rejects them. Part 1 of the 2026-03-02
export, sent by hand to the test tenant `SDNYTEST`, had all 5,019 zero
rows refused with `Value must be positive number in field [orderQty]`
while the rest of the file loaded (see the upload ledger).

**Decision (2026-09-29): size them from the trader's position.** The
orders table alone cannot do it: of the 71,865 full-position stops placed
on 2026-03-02, only 13,167 (18%) ever triggered, and only those carry a
real size on their post-trigger rows; the other 82% (37,036 canceled
while armed, 19,604 reduce-only canceled, 1,997 sibling-canceled, ...)
never show one. Allium's `DEX.TRADES` does carry each side's start
position on every fill (`BUYER_START_POSITION`, `SELLER_START_POSITION`),
so a trader's position at any moment is the latest earlier fill's start
position plus (buyer) or minus (seller) its amount, with no
reconstruction. The `position_sizes` CTE takes, for every zero-size order
in the window, the latest perp fill by that trader in that market at or
before `ORDER_TIMESTAMP` within `position_lookback_days` (default 30) and
uses `ABS(position)` as the size. Several fills often share one block
timestamp and Allium gives no order inside the block, so the lookup
prefers the block's tail: the fill whose resulting position is no other
fill's start position in that block, which is the position after the
block (verified on a two-day timeline: 34 blocks, 15 with several fills,
one tail each, positions chaining across all 33 boundaries). Coverage on 2026-03-02: 71,705 of the
71,865 orders (99.8%) had such a fill, 71,513 (99.5%) held a nonzero
position at placement, 192 were flat, 160 had no fill in 30 days; the
median gap between the last fill and the stop is under an hour, since
traders place the stop right after the entry.

For a sized row `OrderQty` and `LeavesQty` are the position size,
`CumQty` stays 0, `Notional` is the position size times `LIMIT_PRICE`,
and aux `_PositionSize` carries the value; rows after the trigger keep
the real `ORIGINAL_SIZE` the source gives them, so the quantity can differ
between an order's armed and fired versions (Hyperliquid defines it as
"whatever the position is when I fire"; HALO models order versions). The
few rows still at 0 are withheld from the HALO file by default
(`--drop-zero-qty`, counted in `OrdersExportResult.zero_qty_rows`; `--keep-zero-qty`
ships them, which HALO refuses). This is the one place the orders feed
reads the trades table.

### 5.9 Fired trigger orders are re-stamped

When a trigger order fires, Hyperliquid re-places it as a live order under
the **same `oid`** and Allium records the rows after `triggered` with
`ORDER_TIMESTAMP` = the trigger time, `IS_TRIGGER = false`,
`TRIGGER_PRICE = 0`, `TRIGGER_CONDITION = 'Triggered'` and
`TIME_IN_FORCE = 'Gtc'`, while `TYPE` stays `Stop Limit` (or the other
three trigger types). Order 517396971150 shows the pattern: `open` and
`triggered` with `ORDER_TIMESTAMP` 2026-08-16 00:45:06, then
`reduceOnlyCanceled` with `ORDER_TIMESTAMP` 2026-08-20 08:13:12, the
moment it triggered.

Three things follow. The day window on `ORDER_TIMESTAMP` splits such an
order: the armed rows land in the placement day's export and the
post-trigger rows in the trigger day's. `OrigTransactTime` taken from the
row's own `ORDER_TIMESTAMP` would point at the trigger, not the
placement. And the OCO pairing key (§5.2) would no longer match the
`open` rows of the pair. Orders that trigger on their placement day are
unaffected; in the 10-minute profile every post-trigger row still had an
armed row that day.

**Decision (2026-09-28):** the `armed_orders` CTE scans `RAW.ORDERS` for
the armed rows (`IS_TRIGGER = true`) of every trigger order placed in
`[start - trigger_lookback_days, end)` and keeps, per `oid`, the trigger
price of the latest armed row (`MAX_BY`), the placement time
(`MIN(ORDER_TIMESTAMP)`) and the pairing keys. The main query joins it on
`ORDER_ID`: `StopPx` is the looked-up trigger price, `OrigTransactTime`
uses the looked-up placement time, and `tpsl_pairs` is built from the
same CTE so a pair placed before the window still pairs. A trigger-type
row whose order has no armed row inside the lookback degrades to the
plain `Market` / `Limit` it is executing as, which keeps the file valid
(HALO requires `StopPx` on `StopLoss` / `LimitToStop`); the raw `TYPE`
stays in aux `_RawType`. The lookback defaults to 14 days
(`--trigger-lookback-days`, `OrdersQueryParams.trigger_lookback_days`) and
costs one extra scan of `RAW.ORDERS` over that range (a one-account day
export ran in 66 s with it).

How far back the armed rows sit, from the 66,486 `triggered` rows of
2026-08-20: 95.8% triggered within a day of placement, 97.6% within 7
days, 98.3% within 14, 98.7% within 30; the median is under an hour, the
95th percentile 18 hours, the oldest 227 days. At the default, about 1.7%
of fired trigger orders degrade to the plain type; widening to 30 days
buys 0.4 points.

### 5.10 Quote tokens HALO cannot price (USDE)

Hyperliquid quotes its markets in four dollar tokens: USDC (main dex and
most spot), USDH (`km`, `flx`, `vntl` dexes), USDT0 (`cash` dex and some
spot) and USDE (`hyna` dex). On the SDNYTEST test upload of 2026-09-29,
every order row quoted in USDE (12,371 in part 1, `BTC-HYNA/USDE` and the
other `hyna` markets) was refused with `Error getting market data for
symbol USDE`, including rows that carry no `Price`; USDH and USDT0 rows
loaded. HALO resolves the quote token of an instrument against its
market data to express values in USD, and USDE is not there.

The executions feed ships the same `-HYNA/USDE` symbols and they load
(234,378 such rows in HLRESEARCH's April 20 to 26 window). The visible
difference is `Notional`: executions carry Allium's `USD_AMOUNT`, and
HALO's schema says that when `Notional` is present "Solidus won't
calculate market rate externally".

**Decision (2026-09-29):** populate `Notional` as `ORIGINAL_SIZE ×
LIMIT_PRICE` on every row (§6, §8.1 #21). All four quote tokens are
dollar-pegged, so the product is USD to within the peg, and the source
always has both inputs, including on the `Market` / `StopLoss` rows whose
`Price` is withheld. Whether this removes the lookup is to be proven on a
small SDNYTEST file with USDE rows; asking Solidus to add USDE to their
market data stays worthwhile regardless (§8.2 #13).

## 6. Column-by-column mapping (HALO order file)

| HALO column          | Source                                          | Notes |
|----------------------|-------------------------------------------------|-------|
| `TransactTime`       | `IFF(STATUS='open', ORDER_TIMESTAMP, STATUS_CHANGE_TIMESTAMP)` → epoch ms | Block time. Rows sharing `Id` and `TransactTime` are ordered by lifecycle rank (§2). |
| `Id`                 | `ORDER_ID \|\| '-B'` (Buy) / `ORDER_ID \|\| '-S'` (Sell) | HL `oid` plus the side suffix production puts on execution `OrderID`, so a strict-mode execution (`oid-B`) links to its order inside HALO. Each order has one side, so the suffix is deterministic; the raw `oid` is `Id` without its last two characters. Shared across all status-event rows for the same order. Changed 2026-09-28 (was the bare `oid`). |
| `Symbol`             | derived from `COIN` (§3), `@N` resolved through the `spot_pairs` CTE (§5.1) | ≤ 41 chars. |
| `Side`               | `SIDE` mapped via §4.1                          | |
| `OrderQty`           | `ORIGINAL_SIZE`; for full-position TP/SL rows (`ORIGINAL_SIZE = 0`) the trader's position at placement from `position_sizes` | Originally placed size, stable across status events. See §5.8 for the position lookup and the rows withheld when it fails. Changed 2026-09-29. |
| `OrdType`            | `TYPE` mapped via §4.2; a trigger type with no armed row inside the trigger lookback degrades to `Market` / `Limit` | See §5.9. The raw `TYPE` is in aux `_RawType`. |
| `Price`              | `LIMIT_PRICE` when `OrdType ∈ {Limit, LimitToStop}` else `NULL` | Required for `Limit`. Left NULL for `Market` and `StopLoss` on Solidus's instruction (confirmed 2026-09-28), although `LIMIT_PRICE` is populated on every source row (a Hyperliquid market order carries its slippage cap as a limit price). |
| `Status`             | `STATUS` mapped via §4.4                        | `filled` rows are filtered out (fills are covered by the trades pipeline). |
| `OrderCapacity`      | literal `'Agency'`                              | Every DEX order is effectively agency. |
| `Account`            | `USER`                                          | On-chain address. |
| `ClientId`           | `USER`                                          | HL has no separate beneficiary id. |
| `OriginationTrader`  | `USER`                                          | |
| `StopPx`             | `armed_orders.trigger_px` (the order's latest armed `TRIGGER_PRICE`, looked up over the trigger lookback) for trigger-type rows | Required for `StopLoss` / `LimitToStop`. After `triggered` the source row has `IS_TRIGGER=false` and `TRIGGER_PRICE=0` while `TYPE` stays `Stop Market` etc., and the armed rows may sit days earlier (§5.9), so a plain `IFF(IS_TRIGGER, ...)` left those rows without the mandatory `StopPx`. Changed 2026-09-28. |
| `CumQty`             | `ORIGINAL_SIZE - SIZE` in `NUMBER(38,12)`, trailing zeros trimmed | Exact decimal. DOUBLE arithmetic produced artifacts such as `0.060999999999999999` (18 decimals), breaching HALO's 12-decimal cap. Changed 2026-09-28. |
| `LeavesQty`          | `SIZE`; the position size on sized full-position rows (§5.8) | Remaining at event time. `OrderQty = CumQty + LeavesQty` holds on every row. |
| `TimeInForce`        | `TIME_IN_FORCE` mapped via §4.3                 | |
| `OrigTransactTime`   | `COALESCE(armed_orders.placed_ts, ORDER_TIMESTAMP)` when `Status != 'New'` else `NULL` | Required for all non-New rows. The lookup restores the placement time on re-stamped post-trigger rows (§5.9). |
| `ContingencyType`    | `'OCO'` when the order is a trigger type and the `tpsl_pairs` CTE finds a Stop / Take Profit sibling (same `USER`, `COIN`, `SIDE` and placement time); else `NULL` | Sibling pairing over the trigger lookback, not the position flag. See §5.2 and §5.9. Changed 2026-09-28. |
| `ParentOrderId`      | `NULL`                                          | HL TWAP children are not currently surfaced in the orders table; only parent rows are present. |
| `ExVenue`            | literal `'Hyperliquid'`                         | |
| `TrdType`            | per §4.3 (`'PostOnly'` for ALO else `'RegularTrade'`) | |
| `SecurityType`       | per §3                                          | |
| `ExchangeSymbol`     | `Hyperliquid:{PAIR}` for resolved spot, else `Hyperliquid:{COIN}` (§3, §5.1) | Matches the executions feed's `Hyperliquid:{PAIR}`. |
| `ContractMultiplier` | per §3                                          | |
| `Blockchain`         | literal `'ethereum'`                            | HALO's supported Blockchain list has no Hyperliquid value; `ethereum` is the closest valid one (HL accounts are EVM addresses) and matches the executions feed. Changed 2026-09-21 (was `'hyperliquid'`, an invalid enum value). |
| `SymbolType`         | `'Crypto'` for spot; per-market map value for perps (keyed on `COIN`, e.g. `BTC`, `xyz:TSLA`); `NULL` when unmapped | Same seeded map as the executions feed (`symbol_type_map.py`, mirrored from production `R__06b`). No guessed fallback. |
| `WalletAddress`      | `USER`                                          | |
| `Text`               | `STATUS` (raw)                                  | The specific Hyperliquid reject / cancel reason (`badAloPxRejected`, `siblingFilledCanceled`, ...) that the `Status` collapse discards, visible inside HALO rather than only in aux. camelCase letters, within HALO's charset and 2048-char cap. Added 2026-09-28. |
| `Notional`           | `OrderQty × LIMIT_PRICE` in `NUMBER(38,12)` (the position size on sized full-position rows), trailing zeros trimmed | USD value of the order; every Hyperliquid quote token is dollar-pegged. Sent on every row, including `Market` / `StopLoss` rows whose `Price` is withheld, because without it HALO resolves the quote token against its market data and refuses USDE-quoted instruments (§5.10). Added 2026-09-29 (was `NULL`). |
| `ExpireDateTime`     | `NULL`                                          | HL has no GTD expiry on regular orders. |
| `BUIdentifier`       | `NULL`                                          | Not available. |
| `IpAddress`          | `NULL`                                          | Not available. |

## 7. Supplementary (aux) fields

Aux rows joined back to the HALO row via the same `Id` (side-suffixed,
§6) plus `TransactTime` (since a single `Id` may appear in multiple HALO
rows).

| Aux column         | Source                              | Purpose |
|--------------------|-------------------------------------|---------|
| `_UniqueId`        | `UNIQUE_ID`                         | Allium's row-unique composite key (`status_change_timestamp-…-status-…-order_id-…`). |
| `_RawStatus`       | `STATUS`                            | Full HL status; the same value as HALO `Text` since 2026-09-28, kept so aux stays self-contained. |
| `_RawSide`         | `SIDE`                              | `A` / `B`. |
| `_RawType`         | `TYPE`                              | Preserves the otherwise-lossy `Take Profit *` distinction (Stop and Take Profit both collapse to HALO `StopLoss` / `LimitToStop`). |
| `_RawTif`          | `TIME_IN_FORCE`                     | Includes `Alo` / `FrontendMarket` / `LiquidationMarket`. |
| `_Coin`            | `COIN`                              | Raw HL identifier (preserves `@N`, `xyz:*`, etc.). |
| `_ClientOrderId`   | `CLIENT_ORDER_ID`                   | HL `cloid`. |
| `_IsTrigger`       | `IS_TRIGGER`                        | |
| `_IsTpSl`          | `IS_TAKE_PROFIT_OR_STOP_LOSS`       | HL `isPositionTpsl`: sized to the position. Not the OCO marker (§5.2). |
| `_PositionSize`    | `position_sizes.position_size` when `ORIGINAL_SIZE = 0` | The trader's position at placement that became `OrderQty` on a full-position TP/SL row (§5.8); empty otherwise. |
| `_IsReduceOnly`    | `IS_REDUCE_ONLY`                    | |
| `_TriggerCondition`| `TRIGGER_CONDITION`                 | Human-readable string (`Price above 86672`). |
| `_TriggerPrice`    | `TRIGGER_PRICE`                     | Numeric trigger price (also mapped to HALO `StopPx`). |
| `_Children`        | `CHILDREN`                          | Raw JSON of TP/SL OCO children when present. |
| `_BuilderAddress`  | `BUILDER_ADDRESS`                   | |
| `_BuilderFee`      | `BUILDER_FEE`                       | |
| `_OrderTimestamp`  | `ORDER_TIMESTAMP`                   | Original placement (also in HALO `OrigTransactTime`). |
| `_StatusChangeTimestamp` | `STATUS_CHANGE_TIMESTAMP`     | This event's timestamp. |

## 8. Decisions and follow-ups

A consolidated log of mapping choices that traded fidelity for
self-containment, plus the work deferred to later passes.

### 8.1 Decisions taken on this pass

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | **`filled` order rows are filtered out** of the orders feed. | Fills are already covered by the trades pipeline (`DEX.TRADES` → HALO executions). HALO order `Status` does not accept `Filled` regardless. |
| 2 | **`triggered` → HALO `Replaced`.** | Closest semantic to "the conditional order has been swapped for a live limit." Alternative was `Restate`. |
| 3 | **ALO → `TimeInForce=GoodTillCancel` + `TrdType=PostOnly`.** | HALO has no ALO enum; post-only intent is carried by `TrdType`. |
| 4 | **`Stop Market` and `Take Profit Market` both map to `StopLoss`** (and the Limit variants to `LimitToStop`). | HALO has no separate take-profit enum. Direction is implicit in `StopPx` vs. mid. The raw HL `TYPE` is preserved in aux `_RawType`. |
| 5 | **`Vault Close` rows are dropped at source.** | Vaults are not in surveillance scope today, and a forced unwind is not a meaningful "order" from a TS-algo perspective. See 8.2#1. |
| 6 | **HIP-3 `{dex}:{market}` emits `{market}-{DEX}/{quote}`** (e.g. `SP500-XYZ/USDC`), superseding the earlier verbatim `xyz:SP500/USDC` form (2026-09-21). | Same symbology as the executions feed and production, so orders and executions of one contract share a `Symbol`; the dex stays in the name because HIP-3 market names are not unique across builder dexes. See §5.4. |
| 7 | **`@N` spot pairs are resolved to `TOKEN_A/TOKEN_B` through a `DEX.TRADES` lookback join** (2026-09-21; superseding the `{COIN}/USDC` placeholder, which now only remains for pairs with no naming trade in the window). | Uses Allium's own token strings, so orders and executions share `Symbol` and `ExchangeSymbol` per pair. See §5.1. |
| 8 | **TP/SL brackets stay inline** — one HALO row per parent with `ContingencyType=OCO`. | Preserves row counts. The raw children JSON is kept in aux `_Children` so per-leg details are still recoverable downstream. |
| 9 | **`Blockchain = 'ethereum'`** (2026-09-21; was `'hyperliquid'`). | HALO's Blockchain enum has no Hyperliquid value; `ethereum` is the value the executions feed and production emit. |
| 10 | **`SymbolType` emitted from the shared per-market map** (2026-09-21). | Optional HALO enum; using the same map as executions keeps both feeds describing an instrument identically. Unmapped perps stay empty. |
| 11 | **`Id` carries the `-B` / `-S` side suffix** (2026-09-28; was the bare `oid`). | Production's executions ship `OrderID` as `oid-B` / `oid-S` and HALO links an execution to its order through that field, so the orders feed must use the same form. The executions feed is unchanged. See §6. |
| 12 | **`#N` HIP-4 outcome-market rows are dropped at source** (2026-09-28). | Out of scope; absent from Allium's `DEX.TRADES` enrichment; would otherwise ship as a mislabeled perp. See §5.7. |
| 13 | **Rows sharing `Id` and `TransactTime` are ordered New, Replaced, then terminal** (2026-09-28). | Block-granular timestamps put 7% of rows on the same millisecond as another row of the same order; without the rank HALO could see `Canceled` before `New`. See §2. |
| 14 | **`Text` carries the raw HL status** (2026-09-28). | The seven-plus reject reasons and six-plus cancel reasons collapse into `Rejected` / `Canceled`; an analyst inside HALO could not recover them from aux. See §6. |
| 15 | **`CumQty` computed in `NUMBER(38,12)`, not DOUBLE** (2026-09-28). | DOUBLE subtraction yields 18-decimal binary artifacts that breach HALO's 12-decimal cap. See §6. |
| 16 | **`Price` stays NULL for `Market` and `StopLoss` rows** (confirmed with Solidus 2026-09-28). | HALO's schema marks `Price` mandatory and the source always has `LIMIT_PRICE`, but Solidus asked for it only on limit-type rows. See §6. |
| 17 | **`StopPx`, `OrigTransactTime` and the OCO keys of fired trigger orders come from the `armed_orders` lookback** (2026-09-28). | Post-trigger rows are re-stamped to the trigger time with `TRIGGER_PRICE = 0`, and their armed rows may sit days earlier, outside the day window. A trigger type with no armed row inside the lookback degrades to `Market` / `Limit`. See §5.9. |
| 18 | **`OCO` from sibling pairing, not `IS_TAKE_PROFIT_OR_STOP_LOSS`** (2026-09-28). | The flag is `isPositionTpsl`; it missed 53% of proven pairs and marked standalone stops. Pairing on user, coin, side and `ORDER_TIMESTAMP` finds 90%. See §5.2. |
| 19 | **Size-0 (full-position TP/SL) rows are kept, counted, and only withheld on request** (2026-09-28). | They hold the fired-stop events; whether HALO accepts `OrderQty = 0` is untested. `--drop-zero-qty` is the fallback, never a silent filter. See §5.8. |
| 20 | **`--exclude-post-only` drops `Alo` rows on request** (2026-09-29; first used for the 2026-03-02 day export). | 98% of a day's events are post-only quoting from about 100 accounts, about 1 TB of CSV per day; the remaining 31 M rows carry every taker, resting and trigger order. Off by default. See §4.3. |
| 22 | **Full-position TP/SL rows are sized from the trader's position at placement** (2026-09-29). | HALO rejects `OrderQty = 0`; the orders table recovers only the 18% that fire; Allium's per-side start positions on fills give the position at placement for 99.5% of them. The rest are withheld and counted. See §5.8. |
| 21 | **`Notional = OrderQty × LIMIT_PRICE` on every row** (2026-09-29; was `NULL`). | HALO refused every USDE-quoted order row on SDNYTEST for lack of market data on USDE; executions with the same symbols load because they carry `Notional`. All four quote tokens are dollar-pegged, so the product is USD to within the peg. To be confirmed on a small SDNYTEST file. See §5.10. |

### 8.2 Follow-ups (revisit later)

1. **Vaults in surveillance scope.** If/when vault activity becomes
   monitored, revisit the `Vault Close` filter — those rows likely need
   their own routing (potentially as `Market` orders flagged in aux),
   and the broader vault-deposit / vault-withdraw flows will need their
   own mapping that does not exist yet.
2. **Spot symbol resolution for `@N`.** Done 2026-09-21 via the
   `DEX.TRADES` lookback join (§5.1). Remaining edge: pairs with no trade
   in the lookback keep the placeholder; widen `--spot-lookback-days` or,
   if that becomes common, add a `spotMeta` snapshot as a second-level
   fallback.
3. **TP/SL leg explosion.** If a TS algo needs per-leg order rows for
   bracket orders, switch from inline to exploded
   (`Id={oid}-tp` / `Id={oid}-sl`, `ParentOrderId={oid}`). Today the
   inline form is sufficient.
4. **HIP-3 contract registry.** Once HIP-3 market launches accelerate,
   it may be worth maintaining a `{perpDex}:{marketName} → canonical
   contract` registry rather than relying on string identity.
5. **Pre-2025-06-08 missing `open` rows.** Out of scope for now. If
   historical coverage before that date becomes a requirement, the
   missing `New` rows for terminal events will need a strategy
   (synthesize from the terminal row, or drop the affected orders).
6. **HIP-3 quote-token map maintenance.** `HIP3_DEX_QUOTE_TOKEN` is a
   hand-kept snapshot. A new dex, or a dex that changes collateral,
   silently falls back to `USDC` and the orders `Symbol` stops matching
   the executions `Symbol` for that dex. Options: derive the map from
   `DEX.TRADES` (`PERP_DEX` → `TOKEN_B_SYMBOL`) at export time, or from
   the `/info` `perpDexs` + `spotMeta` endpoints during the universe
   refresh.
7. **HIP-4 outcome markets.** Excluded today (§5.7). If they come into
   scope, both feeds need work: `DEX.TRADES` has no rows for them, so
   the executions feed would have to read `RAW.FILLS`, and a name source
   for `#N` is required before a HALO `Symbol` can be built.
8. **`OrderQty = 0`: rejected, now sized from the position** (§5.8,
   2026-09-29). Record the withheld counts per day in the upload ledger.
   Still worth telling Solidus: the quantity of a full-position stop is a
   snapshot at placement and the fired rows carry the real size, so
   `OrderQty` can change across an order's versions. Tenant for orders:
   `HLRESEARCH` (decided 2026-09-29); nothing uploaded there yet.
9. **`Alo` as `GoodTillCross`.** HALO v2.1 lists `GoodTillCross` (the
   FIX post-only TIF) in the `TimeInForce` enum, but the onboarding notes
   list only five values. Both feeds send `GoodTillCancel` plus
   `TrdType = PostOnly` until Solidus confirms which field their algos
   read; flip both feeds together, or neither.
11. **Trigger lookback.** `--trigger-lookback-days` defaults to 14, which
    covers 98.3% of fired trigger orders (§5.9); 30 days covers 98.7%.
    Measure how many post-trigger rows still degrade to `Market` / `Limit`
    on real windows and widen it if the share matters; the cost is one
    scan of `RAW.ORDERS` over the lookback.
13. **USDE quote token.** Every `hyna` dex row is refused until either
    the orders feed carries `Notional` (test on SDNYTEST first) or Solidus
    adds USDE to HALO's market data (§5.10). Ask for the latter in any
    case; the executions feed's price-based algos need it too.
12. **Quoting traffic in HALO.** Ask Solidus whether an orders feed of
    1.5 billion events a day (98% post-only quoting, mostly rejected) is
    something HALO should ingest, or whether the 31 M non-post-only rows
    (§4.3) are the intended feed. The answer decides whether
    `--exclude-post-only` becomes the default.
10. **Production port.** When this feed moves into `defi-hyperliquid-halo`,
   it should be its own pipeline (own view, DQ procedure, upload procedure
   with HALO file type `PRIVATE_ORDER_V2`, ledger keyed on
   `RAW.ORDERS.UNIQUE_ID`, and task), separate from the executions
   pipeline, so an orders failure cannot block the executions upload
   (decided 2026-09-28). Two things do not carry over as they are:
   production's `COPY INTO ... SINGLE = FALSE` does not honor the
   lifecycle `ORDER BY` (§2), and a transact-date batch must compute the
   `StopPx` window and the `tpsl_pairs` CTE over `RAW.ORDERS` by
   `ORDER_ID`, not within the batch, because the armed row may have
   shipped earlier.
