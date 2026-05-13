# Functional Spec — Hyperliquid → HALO v2.1 Mapping

This document explains, field by field, how each column in
`ALLIUM_HYPERLIQUID.DEX.TRADES` maps into Solidus's HALO Trade Surveillance
v2.1 Execution Data schema, and the design choices that differ per market
type (spot vs perpetual).

## 1. Row expansion (one trade → two execution records)

Allium stores each trade as a **single row** that already carries both
buyer and seller context (`BUYER_*` / `SELLER_*` columns). HALO v2.1
expects **one execution record per side** so the query UNIONs two CTEs:

- `buy_side` emits the buyer's record with `Side='Buy'`, `Id=TRADE_ID||'-B'`,
  `MatchingID=TRADE_ID||'-S'`, and its own `BUYER_*` attribution fields.
- `sell_side` mirrors this for the seller.

`MatchingID`/`Id` cross-link the two records so HALO can reassemble the
two sides of a given trade.

## 2. Market-type handling

`ALLIUM_HYPERLIQUID.DEX.TRADES.MARKET_TYPE` is either `'spot'` or
`'perpetuals'` — the exporter treats every downstream field through that
lens.

| HALO field            | Perpetual                              | Spot                                               |
|-----------------------|----------------------------------------|----------------------------------------------------|
| `SecurityType`        | `SWAP`                                 | `SPOT` — the authoritative perp/spot marker.       |
| `Symbol`              | `{TOKEN_A_SYMBOL}/{TOKEN_B_SYMBOL}` (e.g. `BTC/USDC`) | Same (e.g. `UBTC/USDC`, `HYPE/USDC`) |
| `ExchangeSymbol`      | `Hyperliquid:{PAIR}` (falls back to `{COIN}`) | Same                                         |
| `ContractMultiplier`  | `'1'` (required for SWAP)              | `NULL`                                             |
| `PositionEffect`      | `OPEN`/`CLOSE` derived from `{side}_DIR` | `NULL` (not applicable — defaults to `CLOSE` in the HALO file to satisfy the HALO CSV null convention) |

`Symbol` is identical across market types — `SecurityType` (SWAP vs SPOT)
is what tells the two apart downstream. `Symbol` is capped at 41
characters by HALO; every currently-listed Hyperliquid pair fits well
under that limit.

### 2.1 PositionEffect ambiguity

`BUYER_DIR` / `SELLER_DIR` is one of: `Open Long`, `Open Short`,
`Close Long`, `Close Short`, `Long > Short`, `Short > Long`. The `>`
variants are position **flips** where one trade both closes the existing
position and opens a new one in the opposite direction. We emit `NULL`
for those rows and let the HALO export defaulting rule (`NULL → CLOSE`)
land them in the CSV as `CLOSE`. Splitting a flip into two separate HALO
rows is possible but materially more complex and changes row counts; the
starter SQL did not do it and the user explicitly chose to keep that
behavior.

## 3. Column-by-column mapping (HALO file)

| HALO column              | Source (buy side)                            | Notes |
|--------------------------|----------------------------------------------|-------|
| `TransactTime`           | `DATE_PART(EPOCH_MILLISECOND, TIMESTAMP)::BIGINT` | ms since epoch (HALO requirement). |
| `Id`                     | `TRADE_ID \|\| '-B'`                         | HALO requires per-side unique ids. |
| `MatchingID`             | `TRADE_ID \|\| '-S'`                         | Cross-reference to the counterparty side. |
| `OrderID`                | `BUYER_ORDER_ID::STRING`                     | Required. |
| `MatchingOrderID`        | `SELLER_ORDER_ID::STRING`                    | Required when `ExecutionType=EXCHANGE`. |
| `ExecutionType`          | literal `'EXCHANGE'`                         | Hyperliquid is a CLOB exchange. |
| `Symbol`                 | see §2                                       | ≤41 chars. |
| `Side`                   | literal `'Buy'` / `'Sell'`                   | HALO enum. |
| `Quantity`               | `AMOUNT::STRING`                             | Base-token units. |
| `Price`                  | `PRICE::STRING`                              | As traded on Hyperliquid. |
| `Notional`               | `USD_AMOUNT::STRING`                         | Allium already computes `amount * price` in USD. |
| `Status`                 | literal `'Filled'`                           | Every Allium row is a fill. |
| `ExVenue`                | literal `'Hyperliquid'`                      | |
| `Account`                | `BUYER_ADDRESS`                              | We use the on-chain address as the HALO account identifier. |
| `MatchingAccount`        | `SELLER_ADDRESS`                             | |
| `ClientId`               | `BUYER_ADDRESS`                              | Same — Hyperliquid has no separate client id. |
| `MatchingClientId`       | `SELLER_ADDRESS`                             | |
| `OriginationTrader`      | `BUYER_ADDRESS`                              | Same rationale. |
| `MatchingOriginationTrader` | `SELLER_ADDRESS`                          | |
| `OrderCapacity`          | literal `'Agency'`                           | HALO default; every DEX order is effectively agency. |
| `MatchingOrderCapacity`  | literal `'Agency'`                           | |
| `TrdType`                | literal `'RegularTrade'`                     | Liquidations still emit `RegularTrade`; liquidation context lives in the aux file via `_LiquidatedUser` etc. |
| `ParentOrderId`          | `BUYER_TWAP_ID` (NULL if blank)              | Groups child executions of a TWAP order. |
| `Blockchain`             | literal `'Hyperliquid'`                      | |
| `WalletAddress`          | `BUYER_ADDRESS`                              | |
| `SecurityType`           | see §2                                       | |
| `ExchangeSymbol`         | see §2                                       | |
| `PositionEffect`         | see §2.1                                     | |
| `ContractMultiplier`     | see §2                                       | |
| `IsMaker`                | `NOT {side}_CROSSED`                         | **Non-HALO column** added at the user's request. ``True`` when the ``Account`` side rested passively (maker); ``False`` when it crossed the spread (taker). Refers to ``Account``, not ``MatchingAccount``. HALO ignores unknown columns, so including it here does not break upload. |

Sell side mirrors the above with `BUYER_*` / `SELLER_*` swapped and
`Id`/`MatchingID` suffixes flipped (`-S` / `-B`).

### 3.1 Fields intentionally omitted from `halo.csv`

The HALO validator flags several optional fields, but Allium has no
source data for them, so including empty columns would both clutter the
CSV and (per the reference project's experience) be parsed as literal
`null` strings by the HALO uploader. Omitted fields:

- `CumQty`, `LeavesQty`, `OrderQty`, `OrderPrice`
- `IpAddress`, `MatchingIpAddress`
- `BUIdentifier`, `LiquidityPoolAddress`
- `TreeVolume`, `CopiedAccount`
- Derivatives: `ExpirationDateTime`, `SettleDateTime`, `PutCall`,
  `StrikePrice`, `StrikeValue`, `FundingRate`, `CFICode`, `ExerciseStyle`
  (Hyperliquid perps never expire and are not options).

## 4. `aux.csv` — supplementary fields

Columns joined to `halo.csv` on `Id`:

| Aux column            | Source                          | Purpose |
|-----------------------|---------------------------------|---------|
| `_IsTaker`            | `{side}_CROSSED`                | Raw taker flag, kept for parity with the upstream project (complement of `IsMaker`, which now lives in `halo.csv`). |
| `_ClosedPnl`          | `{side}_CLOSED_PNL`             | Realized PnL on position close. |
| `_Fee`                | `{side}_FEE`                    | Exchange fee charged to this side. |
| `_StartPosition`      | `{side}_START_POSITION`         | Position size before this fill. |
| `_Direction`          | `{side}_DIR`                    | Raw Allium direction string. |
| `_BuilderFee`         | `{side}_BUILDER_FEE`            | Builder-code fee, if any. |
| `_BuilderAddress`     | `{side}_BUILDER_ADDRESS`        | Builder recipient. |
| `_LiquidatedUser`     | `LIQUIDATED_USER`               | NULL for non-liquidation trades. |
| `_LiquidationMarkPrice` | `LIQUIDATION_MARK_PRICE`      | |
| `_LiquidationMethod`  | `LIQUIDATION_METHOD`            | |
| `_TransactionHash`    | `TRANSACTION_HASH`              | On-chain tx hash. |
| `_SourceTradeId`      | `TRADE_ID`                      | Pre-suffix trade id (useful for joining buy+sell back together). |
| `_MarketType`         | `MARKET_TYPE`                   | `spot` / `perpetuals`. |
| `_Coin`               | `COIN`                          | Allium's coin symbol / spot pair id. |
| `_TokenA`             | `TOKEN_A_SYMBOL`                | Base token. |
| `_TokenB`             | `TOKEN_B_SYMBOL`                | Quote token. |
| `_Pair`               | `PAIR`                          | Pretty pair string (e.g. `HYPE/USDC`). |
| `_PerpDex`            | `PERP_DEX`                      | HIP-3 perp dex name. |
| `_PerpMarketName`     | `PERP_MARKET_NAME`              | HIP-3 market name. |
| `_IsHip3`             | `IS_HIP3`                       | Flag for HIP-3 custom perp dexes. |

## 5. Row-count & integrity checks

Sanity queries you can run against a populated Snowflake table:

```sql
-- Every HALO row has a matched counterparty
SELECT _SourceTradeId, COUNT(*) sides, COUNT(DISTINCT Side) distinct_sides
FROM <staging>
GROUP BY _SourceTradeId
HAVING sides != 2 OR distinct_sides != 2;

-- MatchingID cross-references are symmetric
SELECT b.Id AS buy_id, b.MatchingID AS buy_matching,
       s.Id AS sell_id, s.MatchingID AS sell_matching
FROM <staging> b
JOIN <staging> s
  ON b._SourceTradeId = s._SourceTradeId
 AND b.Side = 'Buy' AND s.Side = 'Sell'
WHERE b.MatchingID != s.Id OR s.MatchingID != b.Id;
```

For the CSV output, use the `validate-schema` skill to verify HALO
conformance:

```bash
validate-schema --csv data/<run>/halo.csv
```
