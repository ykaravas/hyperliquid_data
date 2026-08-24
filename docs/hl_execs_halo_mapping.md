# Hyperliquid → HALO v2.1 Mapping

Field-by-field mapping from Allium's `ALLIUM_HYPERLIQUID.DEX.TRADES` table
into Solidus's HALO Trade Surveillance v2.1 Execution Data schema, plus
the market-type handling that differs between spot and perpetual trades.

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
| `Symbol`              | `{TOKEN_A_SYMBOL}/{TOKEN_B_SYMBOL}` (e.g. `BTC/USDC`) | Same (e.g. `UBTC/USDC`, `HYPE/USDC`) |
| `ExchangeSymbol`      | `Hyperliquid:{PAIR}` (falls back to `{COIN}`) | Same                                         |
| `ContractMultiplier`  | `'1'` (required for SWAP)              | `NULL`                                             |
| `PositionEffect`      | `OPEN` / `CLOSE` derived from `{side}_DIR` | `NULL` (not applicable; HALO stores an empty position effect, see §2.1) |

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
2025-05-23. This portable exporter includes all of them in the output
(their context is preserved in aux); only `Open *`, `Close *`, and
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

## 3. Column-by-column mapping (HALO file)

| HALO column                 | Source (buy side)                            | Notes |
|-----------------------------|----------------------------------------------|-------|
| `TransactTime`              | `DATE_PART(EPOCH_MILLISECOND, TIMESTAMP)::BIGINT` | ms since epoch (HALO requirement). |
| `Id`                        | `REPLACE(UNIQUE_ID, ' ', 'T') \|\| '-B'`     | HALO requires per-side unique ids; `TRADE_ID` alone collides across coins (see §1). |
| `MatchingID`                | `REPLACE(UNIQUE_ID, ' ', 'T') \|\| '-S'`     | Cross-reference to the counterparty side. |
| `OrderID`                   | `BUYER_ORDER_ID::STRING`                     | Required. |
| `MatchingOrderID`           | `SELLER_ORDER_ID::STRING`                    | Required when `ExecutionType = EXCHANGE`. |
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
| `IsMaker`                   | `NOT {side}_CROSSED`                         | **Non-HALO column.** `True` when the `Account` side rested passively (maker); `False` when it crossed the spread (taker). Refers to `Account`, not `MatchingAccount`. HALO ignores unknown columns. |

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
| `_SourceTradeId`        | `TRADE_ID`                      | Pre-suffix trade id (joins buy + sell back together). |
| `_MarketType`           | `MARKET_TYPE`                   | `spot` / `perpetuals`. |
| `_Coin`                 | `COIN`                          | Allium's coin symbol / spot pair id. |
| `_TokenA`               | `TOKEN_A_SYMBOL`                | Base token. |
| `_TokenB`               | `TOKEN_B_SYMBOL`                | Quote token. |
| `_Pair`                 | `PAIR`                          | Pretty pair string (e.g. `HYPE/USDC`). |
| `_PerpDex`              | `PERP_DEX`                      | HIP-3 perp dex name. |
| `_PerpMarketName`       | `PERP_MARKET_NAME`              | HIP-3 market name. |
| `_IsHip3`               | `IS_HIP3`                       | Flag for HIP-3 custom perp dexes. |

## 5. Production pipeline divergences (defi-hyperliquid-halo, 2026-08-24)

The production Snowflake pipeline (repo `defi-hyperliquid-halo`, deployed via
Flyway) implements this same mapping but diverges from this portable exporter
in ways that are deliberate. Recorded here so the two artifacts don't get
conflated:

- **Eligibility gate.** Production excludes whole trades where either side's
  direction is a forced closure (`ILIKE '%Liquidat%'`, which covers the
  borrow-liquidation spellings that don't start with `Liquidated`, plus
  `Auto-Deleveraging`), a `Net Child Vaults` aggregation row, or a
  `Spot Dust Conversion`, and drops trades with unresolved symbols. This
  exporter intentionally keeps all of those rows: it's a research tool, and
  liquidation context lives in aux.
- **SQL NULL hazard (fixed in production 2026-08-24).** `IS_HIP3` is NULL on
  spot rows, so a predicate like `(NOT IS_HIP3 OR PERP_DEX IS NOT NULL)`
  evaluates NULL and silently drops every spot trade. Production shipped with
  that bug and delivered zero spot volume until fixed with
  `NOT (COALESCE(IS_HIP3, FALSE) AND PERP_DEX IS NULL)`. Any predicate
  touching `IS_HIP3` or the `*_DIR` columns must be NULL-safe.
- **DQ guard.** Production runs a fail-closed direction whitelist (DQ-7):
  any batch row whose direction is NULL or outside the known-eligible set
  (`Open/Close Long/Short`, flips, `Settlement`, `Buy`, `Sell`) aborts the
  upload. New Hyperliquid direction strings appear over time; the guard
  forces an explicit mapping decision instead of shipping them unmapped.
- **SymbolType.** Production emits a per-market `SymbolType` from a seeded
  500-row map (`R__06b_symbol_type_map.sql`) generated from the
  `hyperliquid_perps_universe.xlsx` workbook's curated SymbolType column
  (Crypto / Memecoin / Stablecoin / Equity / Commodities / FX / FixedIncome /
  Exotics). Spot rows are always `Crypto` (structural: HL spot is
  crypto-only); a perp missing from the map ships with SymbolType empty, no
  guessed fallback. This exporter does not emit SymbolType (the field is
  optional in HALO); port the map if that changes.
- **HIP-3 symbols.** Production embeds the dex in `Symbol` for HIP-3 markets
  (`SP500-XYZ/USDC`) so the same underlying on two dexes can't collide; this
  exporter emits plain `TOKEN_A/TOKEN_B`, so e.g. `GOOGL/USDC` is ambiguous
  across dexes (use `_Coin` / `_PerpDex` in aux to disambiguate).
- **OrderID suffixes.** Production appends `-B` / `-S` to `OrderID` /
  `MatchingOrderID`. This exporter keeps raw Hyperliquid order ids so
  executions stay joinable to its own orders feed (`halo_orders.csv`).
