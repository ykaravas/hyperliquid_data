# Hyperliquid → HALO v2.1 Mapping

Field-by-field mapping from Allium's `ALLIUM_HYPERLIQUID.DEX.TRADES` table
into Solidus's HALO Trade Surveillance v2.1 Execution Data schema, plus
the market-type handling that differs between spot and perpetual trades.

The mapping mirrors the production Snowflake view
`hyperliquid_v_linked_private_execution_v2` in the `defi-hyperliquid-halo`
repo (`db/migrations/hyperliquid/R__07_source_views.sql`), so a `halo.csv`
exported here carries the same values production ships to HALO. Section 5
lists the few places where this exporter deliberately differs. Parity was
last verified on 2026-09-21 against production at commit `a217fa6`.

## 1. Row expansion (one trade → two execution records)

Allium stores each trade as a **single row** that already carries both
buyer and seller context (`BUYER_*` / `SELLER_*` columns). HALO v2.1
expects **one execution record per side**, so each source row is expanded
into two HALO rows:

- A `Buy` record with `Id = REPLACE(UNIQUE_ID, ' ', 'T') || '-B'`,
  `MatchingID = ... || '-S'`, and the buyer's attribution fields.
- A `Sell` record mirroring the above with `-S` / `-B` and the seller's
  attribution fields.

Ids are built from `UNIQUE_ID` (`TRADE_ID-COIN-TIMESTAMP`), not bare
`TRADE_ID`: `TRADE_ID` is only unique per coin, so an all-market export
would produce colliding ids. The space in `UNIQUE_ID`'s timestamp is
replaced with `T` so the id carries no whitespace. (Changed 2026-08-24
to match the production pipeline; earlier exports used `TRADE_ID`.)

`Id` / `MatchingID` cross-link the two records so HALO can reassemble the
two sides of a given trade.

## 2. Market-type handling

`ALLIUM_HYPERLIQUID.DEX.TRADES.MARKET_TYPE` is either `'spot'` or
`'perpetuals'`. Several HALO fields depend on this distinction:

| HALO field            | Perpetual                              | Spot                                               |
|-----------------------|----------------------------------------|----------------------------------------------------|
| `SecurityType`        | `SWAP`                                 | `SPOT` — the authoritative perp/spot marker.       |
| `Symbol`              | Main dex: `{TOKEN_A_SYMBOL}/{TOKEN_B_SYMBOL}` (`BTC/USDC`). HIP-3: `{TOKEN_A_SYMBOL}-{PERP_DEX upper}/{TOKEN_B_SYMBOL}` (`TSLA-XYZ/USDC`, `1000PEPE-HYNA/USDE`), see §2.3 | `{TOKEN_A_SYMBOL}/{TOKEN_B_SYMBOL}` (`UBTC/USDC`, `HYPE/USDC`) |
| `ExchangeSymbol`      | `Hyperliquid:{PAIR}` (falls back to `{COIN}`; for perps `PAIR` equals `COIN`, e.g. `Hyperliquid:BTC`, `Hyperliquid:xyz:CL`) | `Hyperliquid:{PAIR}` (`Hyperliquid:HYPE/USDC`) |
| `ContractMultiplier`  | `'1'` (required for SWAP)              | `NULL`                                             |
| `PositionEffect`      | `OPEN` / `CLOSE` derived from `{side}_DIR` | `NULL` (not applicable; HALO stores an empty position effect, see §2.1) |
| `SymbolType`          | Per-market value from the seeded map (§2.4); `NULL` when the market is not in the map | `Crypto` always (Hyperliquid spot is crypto-only) |

`Symbol` is identical across market types — `SecurityType` (`SWAP` vs
`SPOT`) is what distinguishes them downstream. HALO caps `Symbol` at 41
characters; every currently-listed Hyperliquid pair fits well under that
limit.

### 2.1 PositionEffect ambiguity

The common `BUYER_DIR` / `SELLER_DIR` values are `Open Long`,
`Open Short`, `Close Long`, `Close Short`, `Long > Short`,
`Short > Long` (perps) and `Buy` / `Sell` (spot). The full Allium
history also carries rarer directions (full-history inventory taken
2026-08-24): `Settlement` (exchange settlement on delisting, mapped to
`CLOSE`), the liquidation family (`Liquidated Cross/Isolated
Long/Short`, `Partial Borrow Liquidation`, `Backstop Borrow
Liquidation`), `Auto-Deleveraging` (ADL of the liquidated account's
profitable counterparty), `Spot Dust Conversion` (exchange dust sweeps,
seen 2024-07 to 2025-03), `Net Child Vaults` (vault-aggregation rows,
not real executions), and roughly 38.5M NULL-dir sides through
2025-05-23. By default this exporter applies the same eligibility gate as
production (§2.5), so the liquidation family, `Auto-Deleveraging`,
`Net Child Vaults`, `Spot Dust Conversion` and NULL-direction sides never
reach `halo.csv`; pass `--include-ineligible` to keep them for research
(their context is preserved in aux). Only `Open *`, `Close *`, and
`Settlement` produce a `PositionEffect`, and every other direction
emits `NULL`. Nothing without clean open/close semantics is guessed.

The `>` variants are position **flips** where one trade both closes the
existing position and opens a new one in the opposite direction. For
those rows,
`PositionEffect` is emitted as `NULL`, and HALO stores it as **empty**:
there is no defaulting. (An earlier revision of this doc claimed HALO
applies a `NULL → CLOSE` default; that rule does not exist. Verified
2026-08-24 against `solidus_uat_eu.strict_events_executions`: flip rows
land with `position_effect = ''` while adjacent Open/Close rows land as
`OPEN`/`CLOSE`, and the v2.1 schema defines no default for the field.)
So flips, roughly 2.6% of perp execution sides, carry no open/close
marker in HALO. If surveillance logic ever keys on `PositionEffect =
OPEN`, consider labeling flips by their dominant leg using
`{side}_START_POSITION` (OPEN when `AMOUNT - |start_position| >
|start_position|`). Splitting a flip into two separate HALO rows is
technically possible but materially more complex and changes row counts.

### 2.2 `ContractMultiplier` is not leverage

`ContractMultiplier = '1'` for HL perps reflects the **contract-to-underlying
ratio**, not the user's leverage. On Hyperliquid, perp sizes are denominated
directly in the underlying asset — `AMOUNT = 0.5` on a BTC perp means 0.5 BTC
of exposure — so the multiplier is structurally 1. Leverage (the ratio of
notional exposure to posted margin) is a separate account-level setting that
does not change the contract definition.

Leverage is **not available** in any current Allium HL table:
`DEX.TRADES` (all 48 columns + the `_EXTRA_FIELDS` VARIANT), `RAW.FILLS`,
and `RAW.ORDERS` carry per-fill economics (`fee`, `closed_pnl`,
`start_position`, etc.) but no `leverage` field. The `ASSETS.*` tables
cover token transfers, not account state. Hyperliquid emits leverage
changes as `updateLeverage` L1 actions, which Allium does not currently
index.

It is **not reliably derivable** from trade rows either — `BUYER_START_POSITION` /
`SELLER_START_POSITION` give position size before the fill, but converting that
to an effective leverage requires the user's margin balance at the same instant,
which is not in this dataset. The authoritative live read is HL's REST `/info`
endpoint (`type: "clearinghouseState"`), which returns current leverage per
asset per user but not a historical record.

If leverage becomes a surveillance need, the correct path is a separate
pipeline (HL `/info` snapshots or an action-stream indexer that captures
`updateLeverage`) and the value should land in **aux** as `_LeverageAtFill`
or similar — **never** in `ContractMultiplier`.

### 2.3 HIP-3 symbols embed the dex

HIP-3 lets independent builders deploy their own perp dexes, and market
names are not globally unique across them: `xyz:TSLA` and `cash:TSLA` are
different contracts with different collateral. Allium exposes the dex in
`PERP_DEX` and the market name in `TOKEN_A_SYMBOL`, so for `IS_HIP3` rows
the mapping emits `{TOKEN_A_SYMBOL}-{UPPER(PERP_DEX)}/{TOKEN_B_SYMBOL}`
(`TSLA-XYZ/USDC`). `TOKEN_B_SYMBOL` is the dex's collateral token and is
not always USDC: on 2026-09-21 `DEX.TRADES` showed `USDC` for xyz, io,
para and mkts, and `USDE` for hyna (`1000PEPE-HYNA/USDE`). `IS_HIP3` is
NULL on spot rows, so the branch tests `COALESCE(IS_HIP3, FALSE)`.

This is the production form (`R__07`), and production's DQ-8 check
requires HIP-3 symbols to match `-[A-Z]+/`. The orders feed derives the
same `Symbol` from `COIN` (`hl_orders_halo_mapping.md` §5.4) so orders and
executions of one contract share a HALO `Symbol`. Changed 2026-09-21;
earlier exports emitted the bare `TOKEN_A/TOKEN_B` (`TSLA/USDC`), which
collided across dexes.

### 2.4 SymbolType comes from the production map

`SymbolType` is an optional HALO enum (orders and executions share one
list). Spot rows always emit `Crypto`: Hyperliquid spot is crypto-only, a
structural fact rather than a guess. Perp rows take the curated
per-market value from `src/hyperliquid_halo/symbol_type_map.py`, a
generated mirror of production's `R__06b_symbol_type_map.sql` (524
markets as of the 2026-09-21 workbook refresh, keyed by the Allium `COIN`
value: `BTC`, `xyz:TSLA`; values used: Crypto, Memecoin, Stablecoin,
Equity, Commodities, FX, FixedIncome, Exotics). Production generates that
SQL from the "All Markets" sheet of `hyperliquid_perps_universe.xlsx`
(research repo `hyperliquid_universe`), whose `SymbolType` column is
derived from the curated Asset Class by `src/apply_halo_annotations.py`.
The mirror was checked ticker-for-ticker against both on 2026-09-21.

A perp missing from the map emits `NULL` (empty in the CSV) and nothing
else, per production policy: no guessed fallback, anything that does not
map cleanly stays uncategorized until the map is regenerated. The map is
a snapshot and goes stale within weeks (the 2026-08-23 map missed 16
markets that traded in September, about 6% of perp trades, mostly
`PONS`; the 2026-09-21 refresh covers them). Production's DQ-9 and this
exporter's mirror of it (§2.6) warn, without blocking, whenever a batch
carries perp rows with an empty `SymbolType`, naming the symbols to add.
Regenerate with:

```bash
python -m hyperliquid_halo.sync_symbol_type_map \
    --source <defi-hyperliquid-halo>/db/migrations/hyperliquid/R__06b_symbol_type_map.sql
```

### 2.5 Eligibility gate (default on)

`build_query` applies production's `hyperliquid_v_source_eligible`
predicates verbatim, so a trade is dropped whole (both sides, preserving
Buy/Sell parity) when:

1. **Its symbol is unresolved.** `TOKEN_A_SYMBOL` or `TOKEN_B_SYMBOL` is
   NULL (in practice new `@N` spot pairs Allium has not mapped yet; about
   1.5% of one day's trades on 2026-09-20), or a HIP-3 row has no
   `PERP_DEX`. The constructed `Symbol` would be NULL and production's DQ-3
   would reject it.
2. **Either side is a forced closure.** Direction `ILIKE '%Liquidat%'`
   (covers `Liquidated Cross/Isolated Long/Short` plus `Partial Borrow
   Liquidation` and `Backstop Borrow Liquidation`, which do not start with
   `Liquidated`) or `Auto-Deleveraging`. Excluded per product decision:
   forced fills, not voluntary executions.
3. **Either side is not a real execution.** `Net Child Vaults` (vault
   aggregation rows) or `Spot Dust Conversion` (exchange dust sweeps).

Two NULL hazards are baked into the predicates: `IS_HIP3` is NULL on spot
rows and must be `COALESCE`d (production shipped zero spot volume until
this was fixed on 2026-08-24), and NULL directions make the `NOT (...)`
predicates evaluate NULL, which `WHERE` treats as not-true, so
NULL-direction sides (Allium history through 2025-05-23) are dropped.
That is intentional.

**What the gate removes in practice** (measured 2026-09-21 on
`DEX.TRADES`): on 2026-09-20 it dropped 73,472 of 4,932,416 trades
(1.49%), every one of them an unresolved `@N` spot pair; no
liquidation-family, ADL, vault or dust direction occurred that day. Over
2026-09-01 to 09-20 (105M trades, 210M sides) the direction-based
exclusions were small: 5,968 `Net Child Vaults` sides, 253 sides in the
liquidation family (`Liquidated Isolated/Cross Short/Long`, `Partial
Borrow Liquidation`), 9 `Auto-Deleveraging`. `Settlement` (680 sides)
passes the gate and maps to `CLOSE`.

**Known gap, inherited from production.** Allium also marks liquidation
trades with `LIQUIDATED_USER` / `LIQUIDATION_METHOD`, and those trades
carry ordinary directions (`Close Long | Close Short`). In the same
twenty days 280,609 trades (10,149 on 2026-09-20 alone) had
`LIQUIDATED_USER` set, versus 253 sides with a `Liquidated ...`
direction string. Production's gate keys on direction strings only, so
essentially all liquidation fills pass it and ship as `RegularTrade`;
this exporter mirrors that on purpose. If the product decision
("forced fills are not voluntary executions") is meant to cover them,
the fix belongs in production's `hyperliquid_v_source_eligible` (add
`LIQUIDATED_USER IS NULL`) and DQ-7 first, then here. Until then the
markers are in aux (`_LiquidatedUser`, `_LiquidationMethod`,
`_LiquidationMarkPrice`) in both modes.

`--include-ineligible` (`QueryParams.include_ineligible=True`) removes
the gate for research exports. With it on, `Symbol` can be empty for
unresolved pairs (use aux `_Coin` / `_Pair`), and liquidation context
(`_LiquidatedUser`, `_LiquidationMarkPrice`, `_LiquidationMethod`) is
populated. Such a file is not what production ships and should not be
uploaded to HALO as-is.

### 2.6 Data-quality checks (production's DQ-1..DQ-9)

Every `export-execs` run evaluates production's nine checks
(`hyperliquid_sp_run_dq`, `R__08`) over the rows as they stream
(`src/hyperliquid_halo/dq.py`), with the same ids, predicates and
severities:

| Check | Asserts | Severity |
|-------|---------|----------|
| DQ-1 | rows = 2 x distinct trades | FAIL |
| DQ-2 | each trade has exactly one Buy and one Sell row | FAIL |
| DQ-3 | `TransactTime`, `Id`, `MatchingID`, `OrderID`, `Side`, `Quantity`, `Price`, `Symbol`, `ExVenue` non-NULL | FAIL |
| DQ-4 | `SecurityType` in (SWAP, SPOT) | FAIL |
| DQ-5 | `LENGTH(Symbol) <= 41` | FAIL |
| DQ-6 | `PositionEffect` NULL only on SPOT rows or flips | FAIL |
| DQ-7 | every direction in the known-eligible set (`Open/Close Long/Short`, flips, `Settlement`, `Buy`, `Sell`); NULL fails | FAIL |
| DQ-8 | HIP-3 symbols match `-[A-Z]+/`; both sides of a trade agree on `Symbol` | FAIL |
| DQ-9 | no SWAP rows with an empty `SymbolType` (map is current); names the top unmapped symbols | WARN |

Under `--halo-strict` a FAIL removes both files and exits non-zero with
the failure payload, the equivalent of production's "DQ fails, nothing
ships". In the default mode the report is logged and printed but never
blocks (with `--include-ineligible`, DQ-3 and DQ-7 are expected to fail:
that is the gate being off). WARN never blocks in either mode, as in
production. The checks run in one pass with constant memory because the
query orders rows by `(TransactTime, _SourceTradeId, Side)`, so the two
sides of a trade are adjacent.

## 3. Column-by-column mapping (HALO file)

| HALO column                 | Source (buy side)                            | Notes |
|-----------------------------|----------------------------------------------|-------|
| `TransactTime`              | `DATE_PART(EPOCH_MILLISECOND, TIMESTAMP)::BIGINT` | ms since epoch (HALO requirement). |
| `Id`                        | `REPLACE(UNIQUE_ID, ' ', 'T') \|\| '-B'`     | HALO requires per-side unique ids; `TRADE_ID` alone collides across coins (see §1). |
| `MatchingID`                | `REPLACE(UNIQUE_ID, ' ', 'T') \|\| '-S'`     | Cross-reference to the counterparty side. |
| `OrderID`                   | `BUYER_ORDER_ID::STRING` (default) / `BUYER_ORDER_ID::STRING \|\| '-B'` (`--halo-strict`) | Required. Default keeps the raw Hyperliquid `oid` so it joins to `halo_orders.csv` `Id`; strict mode reproduces production's side suffix (§3.2). |
| `MatchingOrderID`           | `SELLER_ORDER_ID::STRING` (default) / `... \|\| '-S'` (`--halo-strict`) | Required when `ExecutionType = EXCHANGE`. Same rule as `OrderID`. |
| `ExecutionType`             | literal `'EXCHANGE'`                         | Hyperliquid is a CLOB exchange. |
| `Symbol`                    | see §2                                       | ≤ 41 chars. |
| `Side`                      | literal `'Buy'` / `'Sell'`                   | HALO enum. |
| `Quantity`                  | `AMOUNT::STRING`                             | Base-token units. |
| `Price`                     | `PRICE::STRING`                              | As traded on Hyperliquid. |
| `Notional`                  | `USD_AMOUNT::STRING`                         | Allium pre-computes `amount * price` in USD. |
| `Status`                    | literal `'Filled'`                           | Every Allium row is a successful match. Partial vs. complete fill is not distinguished — strictly speaking, fills that leave order quantity remaining should be `PartiallyFilled`, but HALO execution `Status` is display-only and has no algo impact, so the simplification is safe. Accurate labeling would require joining against `RAW.ORDERS` on `BUYER_ORDER_ID` / `SELLER_ORDER_ID`. `CanceledFill` / `AmendFill` do not apply — HL trades are final. |
| `ExVenue`                   | literal `'Hyperliquid'`                      | |
| `Account`                   | `BUYER_ADDRESS`                              | The on-chain address is used as the HALO account identifier. |
| `MatchingAccount`           | `SELLER_ADDRESS`                             | |
| `ClientId`                  | `BUYER_ADDRESS`                              | Hyperliquid has no separate client id. |
| `MatchingClientId`          | `SELLER_ADDRESS`                             | |
| `OriginationTrader`         | `BUYER_ADDRESS`                              | Same rationale. |
| `MatchingOriginationTrader` | `SELLER_ADDRESS`                             | |
| `OrderCapacity`             | literal `'Agency'`                           | HALO default; every DEX order is effectively agency. |
| `MatchingOrderCapacity`     | literal `'Agency'`                           | |
| `TrdType`                   | literal `'RegularTrade'`                     | Liquidations still emit `RegularTrade`; liquidation context lives in the aux file via `_LiquidatedUser` etc. |
| `ParentOrderId`             | `BUYER_TWAP_ID` (NULL if blank)              | Groups child executions of a TWAP order. |
| `Blockchain`                | literal `'ethereum'`                         | HALO's supported Blockchain list has no `Hyperliquid` value; `ethereum` is the closest valid one (HL accounts are EVM addresses). Changed 2026-08-24 (was `'Hyperliquid'`, an invalid enum value). |
| `WalletAddress`             | `BUYER_ADDRESS`                              | |
| `SecurityType`              | see §2                                       | |
| `ExchangeSymbol`            | see §2                                       | |
| `PositionEffect`            | see §2.1                                     | |
| `ContractMultiplier`        | see §2                                       | |
| `SymbolType`                | see §2.4                                     | Optional HALO enum. `Crypto` for spot; per-market map value for perps; empty when unmapped. |
| `IsMaker`                   | `NOT {side}_CROSSED`                         | **Non-HALO column, default mode only.** `True` when the `Account` side rested passively (maker); `False` when it crossed the spread (taker). Refers to `Account`, not `MatchingAccount`. HALO ignores unknown columns. Dropped from `halo.csv` under `--halo-strict` (§3.2); aux `_IsTaker` keeps the flag. |

Sell side mirrors the above with `BUYER_*` / `SELLER_*` swapped and
`Id` / `MatchingID` suffixes flipped (`-S` / `-B`).

### 3.1 Fields intentionally omitted

The HALO schema defines several optional fields for which Allium has no
source data. Emitting them as empty would clutter the CSV and risks
being parsed as literal `null` strings by the HALO uploader. Omitted:

- `CumQty`, `LeavesQty`, `OrderQty`, `OrderPrice`
- `IpAddress`, `MatchingIpAddress`
- `BUIdentifier`, `LiquidityPoolAddress`
- `TreeVolume`, `CopiedAccount`
- Derivatives-only: `ExpirationDateTime`, `SettleDateTime`, `PutCall`,
  `StrikePrice`, `StrikeValue`, `FundingRate`, `CFICode`, `ExerciseStyle`
  (Hyperliquid perps never expire and are not options).

### 3.2 `--halo-strict`: production's column set exactly

`export-execs --halo-strict` (`QueryParams.halo_strict=True`) makes
`halo.csv` byte-for-byte the shape production ships:

- `OrderID` / `MatchingOrderID` carry the same `-B` / `-S` side suffix as
  `Id` / `MatchingID` (`BUYER_ORDER_ID || '-B'` on the Buy row,
  `SELLER_ORDER_ID || '-S'` on the Sell row, mirrored for the
  counterparty), as production has done since its first commit.
- The non-HALO `IsMaker` column is not written; the file ends at
  `SymbolType`, matching production's 30-column order. The maker flag is
  still in aux as `_IsTaker`.
- The eligibility gate is always on; combining `--halo-strict` with
  `--include-ineligible` is rejected.
- Production's DQ-1..DQ-8 fail closed (§2.6) and the output is packaged as
  production's COPY does: one or more parts per transact date, capped at
  250 MB (`--max-file-mb`), named
  `sdny_LINKED_PRIVATE_EXECUTION_V2_DDMMYYYY_partN.csv` (`--file-prefix`),
  with the aux parts under `aux/`.

Nothing else changes: ids, symbols, `SymbolType`, `PositionEffect` and the
gate are identical in both modes. The default mode exists because this
project also ships an orders feed whose `Id` is the raw `oid`, and HALO
links an execution to its order through `OrderID`; a strict-mode
execution (`oid-B`) does not link to that order row. Use strict mode for
files that go to HALO alongside production output, and the default when
orders and executions from this repo are analysed together.

## 4. Supplementary (aux) fields

Additional fields not part of HALO but useful for downstream analysis,
keyed back to the HALO row via `Id`:

| Aux column              | Source                          | Purpose |
|-------------------------|---------------------------------|---------|
| `_IsTaker`              | `{side}_CROSSED`                | Raw taker flag (complement of `IsMaker` in `halo.csv`). |
| `_ClosedPnl`            | `{side}_CLOSED_PNL`             | Realized PnL on position close. |
| `_Fee`                  | `{side}_FEE`                    | Exchange fee charged to this side. |
| `_StartPosition`        | `{side}_START_POSITION`         | Position size before this fill. |
| `_Direction`            | `{side}_DIR`                    | Raw Allium direction string. |
| `_BuilderFee`           | `{side}_BUILDER_FEE`            | Builder-code fee, if any. |
| `_BuilderAddress`       | `{side}_BUILDER_ADDRESS`        | Builder recipient. |
| `_LiquidatedUser`       | `LIQUIDATED_USER`               | `NULL` for non-liquidation trades. |
| `_LiquidationMarkPrice` | `LIQUIDATION_MARK_PRICE`        | |
| `_LiquidationMethod`    | `LIQUIDATION_METHOD`            | |
| `_TransactionHash`      | `TRANSACTION_HASH`              | On-chain tx hash. |
| `_SourceTradeId`        | `REPLACE(UNIQUE_ID, ' ', 'T')`  | Pre-suffix exec key shared by the `-B` and `-S` rows (joins buy + sell back together). Was `TRADE_ID` before 2026-09-21, which collides across coins in all-market exports and broke the two-sides integrity check. |
| `_TradeId`              | `TRADE_ID`                      | Raw Hyperliquid per-coin trade id (joins to `RAW.FILLS`). Not unique across coins. |
| `_MarketType`           | `MARKET_TYPE`                   | `spot` / `perpetuals`. |
| `_Coin`                 | `COIN`                          | Allium's coin symbol / spot pair id. |
| `_TokenA`               | `TOKEN_A_SYMBOL`                | Base token. |
| `_TokenB`               | `TOKEN_B_SYMBOL`                | Quote token. |
| `_Pair`                 | `PAIR`                          | Pretty pair string (e.g. `HYPE/USDC`). |
| `_PerpDex`              | `PERP_DEX`                      | HIP-3 perp dex name. |
| `_PerpMarketName`       | `PERP_MARKET_NAME`              | HIP-3 market name. |
| `_IsHip3`               | `IS_HIP3`                       | Flag for HIP-3 custom perp dexes. |

## 5. Production parity (defi-hyperliquid-halo)

The production Snowflake pipeline (repo `defi-hyperliquid-halo`, deployed
via Flyway) and this portable exporter implement the same mapping. The
table below is the parity status as of 2026-09-21 (production `main` at
`a217fa6`; last Hyperliquid pipeline change `cfa090e`, 2026-08-24).
Aligned items were verified by running both exporters end to end against
`DEX.TRADES` and comparing column order and values.

| Area | Production (`R__07` / `R__08`) | This exporter | Status |
|------|--------------------------------|---------------|--------|
| Eligibility gate | `hyperliquid_v_source_eligible` | Same predicates, on by default; `--include-ineligible` turns it off | Aligned (2026-09-21) |
| `Id` / `MatchingID` | `REPLACE(UNIQUE_ID,' ','T') \|\| '-B'/'-S'` | Same | Aligned (2026-08-24) |
| `Symbol` | `TOKEN_A/TOKEN_B`; HIP-3 `TOKEN_A-DEX/TOKEN_B` | Same | Aligned (2026-09-21) |
| `SymbolType` | 524-row seeded map (2026-09-21 refresh); spot `Crypto`; unmapped perps empty | Same map, mirrored by `sync_symbol_type_map` | Aligned (2026-09-21, checked against the workbook) |
| `PositionEffect` | OPEN / CLOSE / Settlement→CLOSE; NULL otherwise, no defaulting | Same | Aligned (2026-08-24) |
| `Blockchain` | `ethereum` | Same | Aligned (2026-08-24) |
| `ExchangeSymbol`, `ContractMultiplier`, `ParentOrderId`, constants | as documented in §3 | Same | Aligned |
| Column order | `TransactTime` .. `SymbolType` | Same, then `IsMaker` | Aligned |
| `OrderID` / `MatchingOrderID` | `{oid}-B` / `{oid}-S` | raw `{oid}` by default; `{oid}-B` / `{oid}-S` under `--halo-strict` | Aligned under `--halo-strict` (2026-09-21). The default keeps raw ids because this project also ships an orders feed whose `Id` is the raw `oid`, and HALO links an execution to its order through `OrderID`. Production has no orders feed; its suffix has no recorded rationale (present since the first commit). See §3.2. |
| `IsMaker` | not emitted | extra trailing column by default; not written under `--halo-strict` | Aligned under `--halo-strict`. HALO ignores unknown columns either way. |
| Liquidation fills marked by `LIQUIDATED_USER` (ordinary `Close *` directions) | pass the gate, ship as `RegularTrade` | same | Aligned, but a probable production gap (§2.5): 280,609 such trades in 2026-09-01..20 versus 253 `Liquidated ...` direction sides. |
| DQ checks DQ-1..DQ-9 | `R__08`, run before COPY; FAIL aborts, DQ-9 warns | `dq.py`, same checks over the streamed rows; FAIL removes the files under `--halo-strict`, otherwise reported | Aligned (2026-09-21), see §2.6 |
| Per-date COPY into 250 MB parts (`sdny_LINKED_PRIVATE_EXECUTION_V2_DDMMYYYY_partN.csv`) | `R__09` | same layout and names under `--halo-strict` (`exporter._OutputWriter`); aux parts under `aux/` | Aligned (2026-09-21), see §3.2 |
| Upload ledger, HALO PUT, Slack alerts | in `R__09` | not implemented; upload the part files with the `halo-upload` skill | Delivery, out of scope for a CSV exporter. |

Things worth knowing when comparing outputs:

- **Gate effect.** On 2026-09-20 the gate removed 1.49% of trades, all
  unresolved `@N` spot pairs; direction-based exclusions are rare (§2.5).
  Row counts between the two modes differ by roughly that much, and
  liquidation fills marked only by `LIQUIDATED_USER` are in both.
- **Map staleness is shared.** Both sides read the same `SymbolType` map,
  so a market missing here is also missing in production until
  `R__06b` is regenerated from the universe workbook and this mirror is
  re-synced (§2.4). DQ-9 names the gaps on both sides.
- **`_SourceTradeId`** matches production's internal `_source_trade_id`
  (the exec key), not the raw `TRADE_ID`, which lives in `_TradeId`.
