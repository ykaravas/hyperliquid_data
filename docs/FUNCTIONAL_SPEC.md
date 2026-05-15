# Functional Spec — `hyperliquid-halo`

Architecture and algorithm reference for the repo. Two pipelines run side
by side, schematizing Allium's `ALLIUM_HYPERLIQUID` Snowflake data into
Solidus's HALO Trade Surveillance v2.1 ingestion format:

| Feed        | Source table                       | Target HALO schema     | Output files                          |
|-------------|------------------------------------|------------------------|---------------------------------------|
| Executions  | `ALLIUM_HYPERLIQUID.DEX.TRADES`    | v2.1 **Execution Data** | `halo.csv` + `aux.csv`                |
| Orders      | `ALLIUM_HYPERLIQUID.RAW.ORDERS`    | v2.1 **Order Data**     | `halo_orders.csv` + `aux_orders.csv`  |

This file describes **how** the pipelines work — module layout, data
flow, and the algorithmic decisions that aren't obvious from reading
the code. **What** each HALO field is set to lives in the portable
mapping docs:

- [`hl_execs_halo_mapping.md`](hl_execs_halo_mapping.md) — executions, field by field
- [`hl_orders_halo_mapping.md`](hl_orders_halo_mapping.md) — orders, field by field (plus §8 decisions log)

## 1. Module architecture

```
src/hyperliquid_halo/
├── snowflake_client.py      # connection helper (env-driven, supports password / SSO)
├── mapping.py               # executions: SQL template + QueryParams + LIST_MARKETS_SQL
├── exporter.py              # executions: streams query → halo.csv + aux.csv
├── orders_mapping.py        # orders: SQL template + OrdersQueryParams + LIST_ORDER_COINS_SQL
├── orders_exporter.py       # orders: streams query → halo_orders.csv + aux_orders.csv
└── cli.py                   # click CLI, 4 subcommands wiring the above
```

The two feeds are kept in separate modules because their grain and
filter sets differ enough that sharing the SQL builder would obscure
both pipelines without saving much code. They share only the connection
helper.

CLI subcommands:

| Subcommand           | Module(s)                                  | Purpose                                                      |
|----------------------|--------------------------------------------|--------------------------------------------------------------|
| `list-markets`       | `mapping.LIST_MARKETS_SQL`, `exporter.list_markets`    | Summarize trade activity by `(MARKET_TYPE, COIN, PAIR)`.    |
| `export-execs`       | `mapping.build_query`, `exporter.export_to_csv`        | Run the executions mapping and stream the two CSVs.         |
| `list-order-coins`   | `orders_mapping.LIST_ORDER_COINS_SQL`, `orders_exporter.list_order_coins` | Summarize order activity by `COIN` (with inferred market type). |
| `export-orders`      | `orders_mapping.build_orders_query`, `orders_exporter.export_orders_to_csv` | Run the orders mapping and stream the two CSVs.             |

## 2. Pipeline algorithm (shared shape)

Both feeds follow the same three-step pattern:

1. **Build SQL.** A pure function — `build_query` for executions,
   `build_orders_query` for orders — takes a `QueryParams` /
   `OrdersQueryParams` dataclass and returns `(sql, binds)` for the
   Snowflake driver's `pyformat` cursor. The SQL is parameterized by
   date range plus optional filters; the optional filters are
   conditionally appended as `AND <col> = %(<name>)s` clauses so unused
   filters don't appear in the generated SQL at all (keeping the
   Snowflake query planner's job simple).
2. **Stream rows.** The exporter opens a Snowflake cursor, executes
   the query, and pulls results in `fetchmany(fetch_size=10_000)`
   batches. Nothing is materialized in a single list — disk fills up
   on large date ranges but RAM stays bounded.
3. **Write two CSVs.** Each row is split into a HALO row (strict
   v2.1 columns) and an aux row (Hyperliquid-specific extras),
   written to two `csv.DictWriter`s simultaneously. The aux file
   joins back to the HALO file via:
   - **Executions:** `Id` (each HALO row has a unique per-side id).
   - **Orders:** `(Id, TransactTime)` (a given order id appears in
     multiple HALO rows — one per status event — so `Id` alone is
     insufficient).

### 2.1 Cursor-column case handling

Snowflake uppercases unquoted aliases. When the SQL says
`AS TransactTime` the cursor returns column name `TRANSACTTIME`. Both
exporters build an `upper() → cursor-name` map before writing rows so
the PascalCase column lookups still resolve. This was a real regression
the executions exporter hit; the orders exporter inherited the same
defense.

### 2.2 Snowflake pyformat escaping (`%%` in `LIKE` patterns)

The Snowflake connector pre-processes SQL with Python's `%`
substitution. A literal `%` in a `LIKE` pattern (e.g. `COIN LIKE '@%'`)
will be interpreted as a placeholder and raise
`TypeError: not enough arguments for format string`. All `LIKE`
patterns in the SQL templates use **doubled** `%%` so that after
substitution Snowflake sees the intended single `%`. This affects
the orders SQL (COIN-shape inference and status `LIKE '%%Rejected'`)
and the orders filter clauses for `--market-type`.

## 3. Executions feed — row expansion algorithm

Allium stores each match as a **single** `DEX.TRADES` row with
`BUYER_*` and `SELLER_*` columns. HALO v2.1 expects **one execution
record per side**, so the SQL UNIONs two CTEs built from a shared
template (`mapping._side_cte`) that takes parameters for which
prefix is "self" and which is "other":

- `buy_side`: `self=BUYER`, `other=SELLER`, `Side='Buy'`,
  `Id=TRADE_ID||'-B'`, `MatchingID=TRADE_ID||'-S'`.
- `sell_side`: mirror with the prefixes and id suffixes flipped.

`MatchingID` ↔ `Id` cross-link the two HALO rows so surveillance can
reassemble the trade. The shared CTE template guarantees the two sides
cannot drift out of sync — there is exactly one definition of how a
side row is built.

### 3.1 PositionEffect — flips emit NULL → defaulted to CLOSE

`{side}_DIR` is one of: `Open Long`, `Open Short`, `Close Long`,
`Close Short`, `Long > Short`, `Short > Long`. The `>` variants are
**position flips** where one trade both closes the existing position
and opens a new one in the opposite direction. The SQL emits `NULL`
for those rows; the exporter then applies a Python-side defaulting
rule (`_halo_default_position_effect`) that maps `NULL → 'CLOSE'`
only in the HALO file (the aux file preserves the raw NULL).

Splitting flips into two HALO rows is structurally possible but would
change row counts and add real complexity for marginal benefit. The
single-row-with-NULL-then-default approach is the deliberate choice;
see `hl_execs_halo_mapping.md` §2.1.

### 3.2 IsMaker — non-HALO column on `halo.csv`

`halo.csv` carries an `IsMaker` boolean computed as `NOT {side}_CROSSED`,
referring to **`Account`** (not `MatchingAccount`). HALO ignores unknown
columns on upload, so embedding this Hyperliquid-specific flag in the
HALO file is safe. The complementary raw `_IsTaker` flag (= `{side}_CROSSED`)
stays in the aux file for parity with upstream pipelines that consumed it
that way.

## 4. Orders feed — event-sourced grain

`RAW.ORDERS` is **event-sourced**: one row per status change, not one
row per order. A typical order produces 1–3 rows (e.g. `open → canceled`
or `open → triggered → filled`, or a single terminal `*Rejected`).
The mapping emits one HALO order row per source row, with all rows for
the same `ORDER_ID` sharing the same `Id` (HALO models status
transitions of an order this way).

### 4.1 Status enum collapse

Hyperliquid emits 23 distinct status values; HALO's order `Status`
enum has 6 (`New`, `Canceled`, `Restate`, `Replaced`, `Expired`,
`Rejected` — `Filled` / `PartiallyFilled` are execution-only). The
SQL collapses HL → HALO via:

| HL `STATUS`                                                                                                            | HALO `Status`            |
|------------------------------------------------------------------------------------------------------------------------|--------------------------|
| `open`                                                                                                                 | `New`                    |
| `triggered`                                                                                                            | `Replaced`               |
| `filled`                                                                                                               | **filtered out at source** (covered by the executions feed) |
| any HL status ending in `Rejected` (9 variants — `badAloPxRejected`, `perpMarginRejected`, etc.)                       | `Rejected`               |
| anything else (`canceled`, `*Canceled`, `scheduledCancel`, future unknowns)                                            | `Canceled`               |

The collapse is forward-compatible: any new HL status that appears in
the future is bucketed as `Canceled` unless its name ends in `Rejected`.
The original HL status is preserved in aux `_RawStatus` so the
specific rejection / cancel reason is never lost. See
`hl_orders_halo_mapping.md` §4.4.

### 4.2 Filtered-out source rows

The orders SQL filters two row types out at source (in the `WHERE`
clause), before any mapping work:

- **`STATUS = 'filled'`** — fills belong to the executions feed
  (`DEX.TRADES` → HALO Execution Data, with both buyer and seller
  attribution). HALO order `Status` does not accept `Filled` regardless.
- **`TYPE = 'Vault Close'`** — non-user-initiated forced vault unwinds.
  Vaults are out of surveillance scope today; revisit if/when they
  become in scope.

See `hl_orders_halo_mapping.md` §8 for the full decisions log.

### 4.3 `TransactTime` vs `OrigTransactTime`

For `open` (the New row): `TransactTime = ORDER_TIMESTAMP`;
`OrigTransactTime = NULL`.

For all other status events: `TransactTime = STATUS_CHANGE_TIMESTAMP`;
`OrigTransactTime = ORDER_TIMESTAMP` (HALO requires every non-New row
to point back at the original placement time).

### 4.4 ALO → `TimeInForce = GoodTillCancel` + `TrdType = PostOnly`

HL's `Alo` (Add Liquidity Only — post-only) is mapped via two HALO
fields rather than one: the TIF collapses to `GoodTillCancel` (HALO has
no ALO enum), and `TrdType = PostOnly` carries the post-only intent.
The raw `Alo` is preserved in aux `_RawTif`. ALO accounts for ~97% of
all order events in production — surveillance models should weight
this accordingly.

### 4.5 `Symbol` derivation across 5 `COIN` shapes

`RAW.ORDERS` has no `MARKET_TYPE` / `TOKEN_A_SYMBOL` columns — `COIN`
is the only instrument identifier and uses five distinct formats:

| `COIN` shape          | Example       | Emitted `Symbol`          | `SecurityType` |
|-----------------------|---------------|---------------------------|----------------|
| Plain                 | `BTC`         | `BTC/USDC`                | `SWAP`         |
| `k`-prefix            | `kPEPE`       | `kPEPE/USDC`              | `SWAP`         |
| HIP-3 `{dex}:{name}`  | `xyz:SP500`   | `xyz:SP500/USDC`          | `SWAP`         |
| `@N` spot index       | `@107`        | `@107/USDC` (placeholder) | `SPOT`         |
| `base/quote`          | `PURR/USDC`   | `PURR/USDC`               | `SPOT`         |

The HIP-3 prefix is deliberately retained: HIP-3 lets independent
builders deploy their own perp dexes, and market names are **not**
globally unique. Collapsing `xyz:SP500/USDC` and a hypothetical
`abc:SP500/USDC` to one `Symbol` would treat them as the same venue,
which is wrong.

`@N` spot indices are an open issue — resolving them to a real
`base/quote` requires either a join against `DEX.TRADES` or a live
call to HL's `/info → spotMeta`. Today the placeholder is emitted and
the raw `@N` is preserved in aux `_Coin` for downstream reconciliation.

### 4.6 TP/SL bracket orders (OCO)

When a user attaches a TP and an SL to a position, HL records the
bracket as a single order whose `CHILDREN` JSON contains both legs
(both sharing the parent's `oid`). The mapping flags these as
`ContingencyType = OCO` whenever `IS_TAKE_PROFIT_OR_STOP_LOSS = true`
and preserves the raw `CHILDREN` JSON verbatim in aux `_Children`.
Brackets are **not** exploded into separate HALO rows today; doing so
would change row counts. See `hl_orders_halo_mapping.md` §5.2.

### 4.7 Market-type filter via COIN-shape inference

The `--market-type` filter on `export-orders` has no native column to
filter on, so it's translated to a `COIN`-shape predicate:

- `--market-type spot` → `AND (COIN LIKE '@%%' OR COIN LIKE '%%/%%')`
- `--market-type perpetuals` → `AND NOT (COIN LIKE '@%%' OR COIN LIKE '%%/%%')`

(The doubled `%%` is the pyformat escape from §2.2.)

## 5. Cross-cutting decisions

### 5.1 `ContractMultiplier = '1'` for perps — not leverage

For HL perpetuals the mapping emits `ContractMultiplier = '1'`. This
reflects the **contract-to-underlying ratio** (HL perps are denominated
directly in the underlying asset; `AMOUNT = 0.5` on a BTC perp = 0.5 BTC
of exposure), not the user's leverage. Leverage is a separate
account-level setting and is **not available** in any Allium HL table
nor reliably derivable from trade rows. If leverage ever becomes a
surveillance need, the path is a separate `/info` or action-stream
pipeline with the value landing in aux as `_LeverageAtFill` (never in
`ContractMultiplier`). See `hl_execs_halo_mapping.md` §2.2.

### 5.2 `Status = 'Filled'` for all executions — display only

Every `DEX.TRADES` row gets `Status = 'Filled'` even though some
rows are strictly partial fills of larger orders (which would
technically be `PartiallyFilled`). HALO execution `Status` is
display-only and has no algo impact, so the simplification is safe.
Accurate labeling would require joining the fill against `RAW.ORDERS`
on `BUYER_ORDER_ID` / `SELLER_ORDER_ID` to compare against the order's
`ORIGINAL_SIZE` — the cost isn't justified for a field the algos
ignore. `CanceledFill` / `AmendFill` don't apply to HL: on-chain CLOB
trades are final.

## 6. Output file conventions

| File                | Schema                                                        | Join key (back to aux)       |
|---------------------|---------------------------------------------------------------|------------------------------|
| `halo.csv`          | `mapping.HALO_COLUMNS` — strict v2.1 Execution Data + `IsMaker`| `Id`                         |
| `aux.csv`           | `mapping.AUX_COLUMNS`                                         | (joined on `Id`)             |
| `halo_orders.csv`   | `orders_mapping.HALO_ORDER_COLUMNS` — strict v2.1 Order Data  | `(Id, TransactTime)`         |
| `aux_orders.csv`    | `orders_mapping.AUX_ORDER_COLUMNS`                            | (joined on `Id, TransactTime`)|

The orders aux file leads with both `Id` and `TransactTime` as the
join key because a single order id appears in multiple HALO rows
(one per status event), unlike executions where each HALO row's `Id`
is unique.

Default filenames are distinct (`halo.csv` vs. `halo_orders.csv`) so
the two feeds can be exported into the same `--out-dir` without
collision. Both are overridable via `--halo-filename` / `--aux-filename`
on the respective subcommands.

## 7. Integrity checks

Sanity queries you can run against the populated output (treating the
CSV as a staging table or after re-loading into Snowflake):

```sql
-- EXECUTIONS: every HALO row has a matched counterparty
SELECT _SourceTradeId, COUNT(*) sides, COUNT(DISTINCT Side) distinct_sides
FROM <halo_executions_staging>
GROUP BY _SourceTradeId
HAVING sides != 2 OR distinct_sides != 2;

-- EXECUTIONS: MatchingID cross-references are symmetric
SELECT b.Id AS buy_id, b.MatchingID AS buy_matching,
       s.Id AS sell_id, s.MatchingID AS sell_matching
FROM <halo_executions_staging> b
JOIN <halo_executions_staging> s
  ON b._SourceTradeId = s._SourceTradeId
 AND b.Side = 'Buy' AND s.Side = 'Sell'
WHERE b.MatchingID != s.Id OR s.MatchingID != b.Id;

-- ORDERS: every non-New row has an OrigTransactTime
SELECT COUNT(*) AS missing_orig
FROM <halo_orders_staging>
WHERE Status != 'New' AND OrigTransactTime IS NULL;

-- ORDERS: all rows for the same Id should share OrderQty (ORIGINAL_SIZE is stable)
SELECT Id, COUNT(DISTINCT OrderQty) AS distinct_qtys
FROM <halo_orders_staging>
GROUP BY Id
HAVING distinct_qtys > 1;
```

For CSV-level conformance, run the Solidus `validate-schema` skill:

```bash
validate-schema --csv data/<run>/halo.csv
validate-schema --csv data/<run>/halo_orders.csv
```

## 8. Tooling

- **Tests** (`tests/`) use a `_FakeCursor` double — no Snowflake
  connection is opened. Both `mapping` / `orders_mapping` SQL assembly
  and both exporters' CSV writes are covered. Run with `pytest` after
  `pip install -e ".[dev]"`.
- **VS Code launch configurations** (`.vscode/launch.json`) cover each
  CLI subcommand for quick debug runs against `.env`.
- **Snowflake auth** is environment-driven (`SNOWFLAKE_*`), supports
  both password and `SNOWFLAKE_AUTHENTICATOR=externalbrowser` SSO.
  See `snowflake_client.py` for the supported envar set.

## 9. Pointers

For field-level mapping detail and the decisions that drove individual
field choices:

- Executions: [`hl_execs_halo_mapping.md`](hl_execs_halo_mapping.md)
- Orders: [`hl_orders_halo_mapping.md`](hl_orders_halo_mapping.md) (see §8 decisions log)
