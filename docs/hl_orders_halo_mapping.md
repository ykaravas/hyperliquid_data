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
| `xyz:*`      | `SWAP`         | `{COIN}/USDC` (keep `xyz:` prefix; HIP-3 contracts settle in USDC) | `Hyperliquid:{COIN}` | `'1'` |
| `@N`         | `SPOT`         | `{COIN}/USDC` placeholder — needs lookup, see §5.1 | `Hyperliquid:{COIN}` | `NULL` |
| `base/quote` | `SPOT`         | `COIN` (already `base/quote`) | `Hyperliquid:{COIN}` | `NULL` |

> The 41-character HALO `Symbol` cap is comfortable for all observed
> HIP-3 names (`xyz:SP500/USDC` ≈ 14 chars).

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
table alone.

**Decision (current):** to keep this mapping self-contained, the
emitted `Symbol` is `{COIN}/USDC` (literally `@107/USDC`) and the raw
`@N` is preserved in aux `_Coin`. Every HL spot pair is USDC-quoted
today, so the quote side is always correct; only the base is opaque.

**Deferred resolution paths** (for when this becomes blocking):

- Join against `ALLIUM_HYPERLIQUID.DEX.TRADES` (carries `TOKEN_A_SYMBOL` /
  `TOKEN_B_SYMBOL` / `PAIR`) on the matching trade window to build an
  `@N → base` lookup.
- Query Hyperliquid's `/info → spotMeta` REST endpoint directly and
  cache the index → token map.

See §8.

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

**Decision (current):** keep the full identifier as-is in `Symbol`
(e.g. `xyz:SP500/USDC`). The 41-character HALO `Symbol` cap is
comfortable.

**Why keep the prefix:** HIP-3 lets independent builders deploy their
own perp dexes, and market names are **not** globally unique — a future
`abc:SP500` could exist alongside `xyz:SP500` and refer to a completely
different contract. Stripping the dex prefix would collapse those into
one `Symbol`, which surveillance models would treat as a single venue.
Carrying the prefix keeps each HIP-3 contract distinct downstream.

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
| `Symbol`             | derived from `COIN` (§3)                        | ≤ 41 chars. |
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
| `ExchangeSymbol`     | per §3                                          | |
| `ContractMultiplier` | per §3                                          | |
| `Blockchain`         | literal `'hyperliquid'` (lowercase)             | |
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
| 6 | **HIP-3 `xyz:*` identifiers are kept verbatim in `Symbol`** (e.g. `xyz:SP500/USDC`). | HIP-3 market names are not globally unique across builder dexes; stripping the prefix would collapse genuinely-different contracts into one `Symbol`. |
| 7 | **`@N` spot pairs emit `{COIN}/USDC` as a placeholder `Symbol`** (e.g. `@107/USDC`) and the raw `@N` is in aux `_Coin`. | Keeps the mapping self-contained — resolution would require either a join against the trades table or a live call to HL's `/info → spotMeta`. Both are deferred. See 8.2#2. |
| 8 | **TP/SL brackets stay inline** — one HALO row per parent with `ContingencyType=OCO`. | Preserves row counts. The raw children JSON is kept in aux `_Children` so per-leg details are still recoverable downstream. |

### 8.2 Follow-ups (revisit later)

1. **Vaults in surveillance scope.** If/when vault activity becomes
   monitored, revisit the `Vault Close` filter — those rows likely need
   their own routing (potentially as `Market` orders flagged in aux),
   and the broader vault-deposit / vault-withdraw flows will need their
   own mapping that does not exist yet.
2. **Spot symbol resolution for `@N`.** Wire up either a `DEX.TRADES`
   join or a `spotMeta` lookup so the emitted `Symbol` carries the real
   base token (`PURR/USDC` instead of `@8/USDC`, etc.). Until then,
   downstream consumers must reconcile via aux `_Coin`.
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
