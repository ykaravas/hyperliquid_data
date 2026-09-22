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
stays pinned to the original placement.

**HALO consequence.** Each Allium row maps to one HALO order row. All
rows for the same `ORDER_ID` share `Id`. The `New` row uses
`TransactTime = ORDER_TIMESTAMP`; non-`New` rows use
`TransactTime = STATUS_CHANGE_TIMESTAMP` and
`OrigTransactTime = ORDER_TIMESTAMP`.

`filled` status rows are **routed to the executions output**, not the
orders output — HALO order `Status` excludes `Filled`/`PartiallyFilled`.

## 3. `COIN` format — Symbol / SecurityType derivation

`COIN` is the only instrument identifier on the orders table and uses
five distinct formats. Distribution from a 2-hour sample (~660 M rows):

| `COIN` shape         | Example       | Share  | Means                                                  |
|----------------------|---------------|--------|--------------------------------------------------------|
| Plain name           | `BTC`, `ETH`  | 96.5 % | Standard HL perpetual, USDC-margined.                  |
| `k`-prefix           | `kPEPE`       | 1.5 %  | HL "1000×" perpetual (`kPEPE` = 1000 PEPE units).      |
| `xyz:*`              | `xyz:SP500`   | (rare) | HIP-3 builder-deployed perp (`{perpDex}:{marketName}`). See §5.4. |
| `@N` (numeric index) | `@107`        | 1.9 %  | HL spot pair, identified by internal index.            |
| `base/quote`         | `PURR/USDC`   | 0.02 % | Legacy spot pair (`PURR/USDC` only at time of sampling).|

Inferred mapping:

| `COIN` shape | `SecurityType` | `Symbol`          | `ExchangeSymbol`            | `ContractMultiplier` |
|--------------|----------------|-------------------|-----------------------------|----------------------|
| Plain        | `SWAP`         | `{COIN}/USDC`     | `Hyperliquid:{COIN}`        | `'1'`                |
| `k`-prefix   | `SWAP`         | `{COIN}/USDC` (keep `k`) | `Hyperliquid:{COIN}` | `'1'`                |
| `{dex}:*`    | `SWAP`         | `{market}-{DEX}/{quote}` (`xyz:TSLA` → `TSLA-XYZ/USDC`, `hyna:1000PEPE` → `1000PEPE-HYNA/USDE`); quote is the dex's collateral, see §5.4 | `Hyperliquid:{COIN}` | `'1'` |
| `@N`         | `SPOT`         | `{TOKEN_A}/{TOKEN_B}` resolved from `DEX.TRADES` (`@107` → `HYPE/USDC`); `{COIN}/USDC` placeholder only when the pair had no trade in the lookback, see §5.1 | `Hyperliquid:{PAIR}` when resolved (`Hyperliquid:HYPE/USDC`), else `Hyperliquid:{COIN}` | `NULL` |
| `base/quote` | `SPOT`         | `COIN` (already `base/quote`) | `Hyperliquid:{COIN}` | `NULL` |

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
lives in aux `_RawStatus`.

Volume-wise, `badAloPxRejected` dominates (≈59 % of all order events):
post-only orders that would have crossed the spread are rejected
immediately. Surveillance models should weight this accordingly.

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

### 5.2 TP/SL bracket orders (OCO)

When a user attaches a TP and an SL to a position, Hyperliquid records
the bracket as a **single** order with two entries inside `CHILDREN`
(both children share the parent's `oid`). A sampled CHILDREN payload:

```json
[
  {"orderType": "Stop Market",       "triggerCondition": "Price below 77.208", "triggerPx": "77.208", "side": "A", "oid": 427030901544, ...},
  {"orderType": "Take Profit Market","triggerCondition": "Price above 82",     "triggerPx": "82.0",   "side": "A", "oid": 427030901544, ...}
]
```

**Decision (current):** keep brackets **inline** — one HALO order row
per parent, flagged with `ContingencyType = OCO` whenever
`IS_TAKE_PROFIT_OR_STOP_LOSS = true` or `CHILDREN` has ≥ 2 elements.
The raw `CHILDREN` JSON is preserved verbatim in aux `_Children`, so
each leg's `orderType` / `triggerPx` / `triggerCondition` / `side` is
still recoverable downstream without us inflating row counts or
inventing synthetic ids.

Exploding each child into its own HALO row (e.g. `Id={oid}-tp`,
`Id={oid}-sl`, `ParentOrderId={oid}`) is possible later if a TS algo
explicitly needs per-leg rows, but it changes counts and is not done
today. See §8.

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

## 6. Column-by-column mapping (HALO order file)

| HALO column          | Source                                          | Notes |
|----------------------|-------------------------------------------------|-------|
| `TransactTime`       | `IFF(STATUS='open', ORDER_TIMESTAMP, STATUS_CHANGE_TIMESTAMP)` → epoch ms | |
| `Id`                 | `ORDER_ID`                                      | HL `oid`. Shared across all status-event rows for the same order. |
| `Symbol`             | derived from `COIN` (§3), `@N` resolved through the `spot_pairs` CTE (§5.1) | ≤ 41 chars. |
| `Side`               | `SIDE` mapped via §4.1                          | |
| `OrderQty`           | `ORIGINAL_SIZE`                                 | Originally placed size, stable across status events. |
| `OrdType`            | `TYPE` mapped via §4.2                          | |
| `Price`              | `LIMIT_PRICE` when `OrdType ∈ {Limit, LimitToStop}` else `NULL` | Required for `Limit`. |
| `Status`             | `STATUS` mapped via §4.4                        | `filled` rows are filtered out (fills are covered by the trades pipeline). |
| `OrderCapacity`      | literal `'Agency'`                              | Every DEX order is effectively agency. |
| `Account`            | `USER`                                          | On-chain address. |
| `ClientId`           | `USER`                                          | HL has no separate beneficiary id. |
| `OriginationTrader`  | `USER`                                          | |
| `StopPx`             | `TRIGGER_PRICE` when `IS_TRIGGER=true`          | Required for `StopLoss` / `LimitToStop`. |
| `CumQty`             | `ORIGINAL_SIZE - SIZE`                          | Computed (cast to numeric first). |
| `LeavesQty`          | `SIZE`                                          | Remaining at event time. |
| `TimeInForce`        | `TIME_IN_FORCE` mapped via §4.3                 | |
| `OrigTransactTime`   | `ORDER_TIMESTAMP` when `Status != 'New'` else `NULL` | Required for all non-New rows. |
| `ContingencyType`    | `'OCO'` when `IS_TAKE_PROFIT_OR_STOP_LOSS=true` else `NULL` | See §5.2. |
| `ParentOrderId`      | `NULL`                                          | HL TWAP children are not currently surfaced in the orders table; only parent rows are present. |
| `ExVenue`            | literal `'Hyperliquid'`                         | |
| `TrdType`            | per §4.3 (`'PostOnly'` for ALO else `'RegularTrade'`) | |
| `SecurityType`       | per §3                                          | |
| `ExchangeSymbol`     | `Hyperliquid:{PAIR}` for resolved spot, else `Hyperliquid:{COIN}` (§3, §5.1) | Matches the executions feed's `Hyperliquid:{PAIR}`. |
| `ContractMultiplier` | per §3                                          | |
| `Blockchain`         | literal `'ethereum'`                            | HALO's supported Blockchain list has no Hyperliquid value; `ethereum` is the closest valid one (HL accounts are EVM addresses) and matches the executions feed. Changed 2026-09-21 (was `'hyperliquid'`, an invalid enum value). |
| `SymbolType`         | `'Crypto'` for spot; per-market map value for perps (keyed on `COIN`, e.g. `BTC`, `xyz:TSLA`); `NULL` when unmapped | Same seeded map as the executions feed (`symbol_type_map.py`, mirrored from production `R__06b`). No guessed fallback. |
| `WalletAddress`      | `USER`                                          | |
| `Notional`           | `NULL`                                          | Not required for crypto; `OrdType=Reverse` (the case that needs Notional) does not exist in HL. |
| `ExpireDateTime`     | `NULL`                                          | HL has no GTD expiry on regular orders. |
| `BUIdentifier`       | `NULL`                                          | Not available. |
| `IpAddress`          | `NULL`                                          | Not available. |

## 7. Supplementary (aux) fields

Aux rows joined back to the HALO row via the same `Id` plus
`OrigTransactTime` (since a single `Id` may appear in multiple HALO
rows).

| Aux column         | Source                              | Purpose |
|--------------------|-------------------------------------|---------|
| `_UniqueId`        | `UNIQUE_ID`                         | Allium's row-unique composite key (`status_change_timestamp-…-status-…-order_id-…`). |
| `_RawStatus`       | `STATUS`                            | Full HL status (preserves the specific reject / cancel reason). |
| `_RawSide`         | `SIDE`                              | `A` / `B`. |
| `_RawType`         | `TYPE`                              | Preserves the otherwise-lossy `Take Profit *` distinction (Stop and Take Profit both collapse to HALO `StopLoss` / `LimitToStop`). |
| `_RawTif`          | `TIME_IN_FORCE`                     | Includes `Alo` / `FrontendMarket` / `LiquidationMarket`. |
| `_Coin`            | `COIN`                              | Raw HL identifier (preserves `@N`, `xyz:*`, etc.). |
| `_ClientOrderId`   | `CLIENT_ORDER_ID`                   | HL `cloid`. |
| `_IsTrigger`       | `IS_TRIGGER`                        | |
| `_IsTpSl`          | `IS_TAKE_PROFIT_OR_STOP_LOSS`       | |
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
