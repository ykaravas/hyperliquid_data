# Functional Spec — `hyperliquid-halo`

Architecture and algorithm reference for the repo. Two pipelines run side
by side, schematizing Allium's `ALLIUM_HYPERLIQUID` Snowflake data into
Solidus's HALO Trade Surveillance v2.1 ingestion format:

| Feed        | Source table                       | Target HALO schema     | Output files                          |
|-------------|------------------------------------|------------------------|---------------------------------------|
| Executions  | `ALLIUM_HYPERLIQUID.DEX.TRADES`    | v2.1 **Execution Data** | `halo.csv` + `aux.csv`                |
| Orders      | `ALLIUM_HYPERLIQUID.RAW.ORDERS`    | v2.1 **Order Data**     | `halo_orders.csv` + `aux_orders.csv`  |

Both feeds mirror the production Snowflake pipeline in the
`defi-hyperliquid-halo` repo (`hyperliquid_v_linked_private_execution_v2`
for executions; production has no orders feed, so the orders feed follows
the same symbology and enum choices). Parity status and the deliberate
divergences are tabulated in `hl_execs_halo_mapping.md` §5.

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
├── symbol_type_map.py       # GENERATED: per-market HALO SymbolType, mirrored from production
├── sync_symbol_type_map.py  # regenerates symbol_type_map.py from production's R__06b SQL
├── dq.py                    # executions: streaming mirror of production's DQ-1..DQ-9
└── cli.py                   # click CLI, 4 subcommands wiring the above
```

`symbol_type_map.py` is data, not logic: a 500-row `(coin, symbol_type)`
tuple plus a helper that renders it as SQL `VALUES` rows. Both SQL
templates splice those rows into a `symbol_type_map` CTE and LEFT JOIN it
on `COIN`, so the lookup happens in Snowflake alongside the rest of the
mapping (no Python-side post-processing of HALO values, same principle as
the removed PositionEffect defaulting in §3.1).

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
  `Id=REPLACE(UNIQUE_ID,' ','T')||'-B'`, `MatchingID=...||'-S'`.
  (`UNIQUE_ID`, not `TRADE_ID`: trade ids are only unique per coin, so
  all-market exports would collide; see the mapping doc §1.)
- `sell_side`: mirror with the prefixes and id suffixes flipped.

`MatchingID` ↔ `Id` cross-link the two HALO rows so surveillance can
reassemble the trade. The shared CTE template guarantees the two sides
cannot drift out of sync — there is exactly one definition of how a
side row is built.

### 3.0 Eligibility gate (production parity, default on)

Before the row expansion, the `eligible` CTE applies production's
`hyperliquid_v_source_eligible` predicates verbatim (`mapping._ELIGIBILITY_SQL`):
unresolved symbols (`TOKEN_A_SYMBOL` / `TOKEN_B_SYMBOL` NULL, or HIP-3
without `PERP_DEX`), forced closures (`ILIKE '%Liquidat%'`,
`Auto-Deleveraging`) and non-executions (`Net Child Vaults`,
`Spot Dust Conversion`) are dropped whole-trade. Two NULL hazards are
handled the way production handles them: `IS_HIP3` is `COALESCE`d
(NULL on spot rows) and NULL directions fall out of the `NOT (...)`
predicates by design. `QueryParams.include_ineligible` (CLI
`--include-ineligible`) removes the gate for research exports; the
predicate block is simply omitted from the rendered SQL, everything
else is unchanged. The `%` characters in the `ILIKE` patterns are doubled
per §2.2. See `hl_execs_halo_mapping.md` §2.5, including the measured
effect (about 1.5% of a day's trades, nearly all unresolved spot pairs)
and the inherited gap: liquidation fills flagged only by
`LIQUIDATED_USER` carry ordinary directions and pass the gate in
production and here.

A `base` CTE then derives the per-trade values once (`exec_key`,
`symbol_str`, `security_type_str`, `exchange_symbol_str`,
`contract_multiplier_str`, `symbol_type_str`) so both side projections
read identical values by construction, the same shape as production's
`base` CTE.

### 3.1 PositionEffect — no defaulting; empty means empty

The SQL maps `Open Long`/`Open Short` to `OPEN` and `Close Long`/
`Close Short`/`Settlement` to `CLOSE`, and emits `NULL` for everything
else: spot rows, position flips (`Long > Short` / `Short > Long`), and
the rare forced-fill directions (liquidations, ADL, dust conversions;
full inventory in `hl_execs_halo_mapping.md` §2.1). The exporter writes
those NULLs to `halo.csv` as empty values.

There used to be a Python-side rule (`_halo_default_position_effect`)
that forced `NULL → 'CLOSE'` in the HALO file. It was removed on
2026-08-24: HALO applies no such default (verified against
`solidus_uat_eu.strict_events_executions`, where NULL uploads land as
empty), so the rule was actively mislabeling every spot row and every
flip as `CLOSE`. Nothing without clean open/close semantics gets a
guessed value.

Splitting flips into two HALO rows is structurally possible but would
change row counts and add real complexity for marginal benefit; a
dominant-leg labeling via `{side}_START_POSITION` is the documented
option if surveillance ever needs flips categorized (see
`hl_execs_halo_mapping.md` §2.1).

### 3.2 IsMaker, a non-HALO column on `halo.csv` (default mode)

`halo.csv` carries an `IsMaker` boolean computed as `NOT {side}_CROSSED`,
referring to **`Account`** (not `MatchingAccount`). HALO ignores unknown
columns on upload, so embedding this Hyperliquid-specific flag in the
HALO file is safe. The complementary raw `_IsTaker` flag (= `{side}_CROSSED`)
stays in the aux file for parity with upstream pipelines that consumed it
that way. Under `--halo-strict` (§3.4) the column is not written.

### 3.5 Data-quality checks mirror production (`dq.py`)

`ExecutionDqChecker` re-implements production's `hyperliquid_sp_run_dq`
(`R__08`) as a single streaming pass: the exporter calls `observe()` per
row while writing and `finish()` once at the end. DQ-3 to DQ-7 and the
pattern half of DQ-8 are per-row counters; DQ-1, DQ-2, the symmetry
half of DQ-8 and DQ-9 need per-trade state, and get it with O(1) memory
because the query orders rows by `(TransactTime, _SourceTradeId, Side)`:
a trade's two rows are adjacent, so the checker only ever holds the
current trade group. Ids, predicates, observed payloads and severities
match production (DQ-1..8 FAIL, DQ-9 WARN).

Consequences per mode:

- `--halo-strict`: a FAIL removes `halo.csv` and `aux.csv`, raises
  `dq.DqFailure` (the CLI prints the report and exits 1). Nothing that
  could be uploaded by mistake is left behind, production's "DQ fails,
  nothing ships".
- default: the report is logged and printed; nothing blocks. With
  `--include-ineligible`, DQ-3 (empty `Symbol` on unresolved pairs) and
  DQ-7 (liquidation directions) fail by construction.
- DQ-9 (`SymbolType` empty on SWAP rows) warns in both modes and names
  the top unmapped symbols, the cue to re-sync the map (§5.3).

`ExportResult.dq_report` carries the full `DqReport` for programmatic
callers.

### 3.4 `--halo-strict`: production's file shape exactly

`QueryParams.halo_strict` (CLI `--halo-strict`) switches two things and
nothing else:

- `_side_cte` renders `OrderID` / `MatchingOrderID` with the same `-B` /
  `-S` side suffix as `Id` / `MatchingID`, production's form. The SQL is
  rendered per call by `_render_sql_template(strict=...)`, so both modes
  share one template and cannot drift.
- The exporter writes `halo.csv` with `mapping.HALO_STRICT_COLUMNS`
  (`HALO_COLUMNS` minus `IsMaker`), i.e. production's 30 columns in
  production order. The SQL still computes `IsMaker`; only the CSV column
  selection changes.

`halo_strict` and `include_ineligible` are mutually exclusive
(`QueryParams.__post_init__` raises; the CLI reports a usage error): a
strict file must contain only what production ships. The default mode
keeps raw order ids because the orders feed's `Id` is the raw `oid` and
HALO links executions to orders through `OrderID`.

Strict mode also packages the output the way production's `R__09` COPY
does (§3.6).

### 3.6 Packaging: per-date parts (`_OutputWriter`)

Production COPYs each transact date separately into part files of at most
250 MB named `sdny_LINKED_PRIVATE_EXECUTION_V2_DDMMYYYY_partN.csv` (well
under HALO's 500 MB per-file limit). `exporter._OutputWriter` reproduces
that: when `max_part_mb` is set (the default in strict mode is
`DEFAULT_PART_MB = 250`; `--max-file-mb` overrides, `--file-prefix`
changes the tenant prefix) it derives each row's transact date from
`TransactTime`, opens `part1` for a new date, and rolls to the next part
once the current HALO part reaches the cap. The size is checked every
1,000 rows by flushing and reading the byte offset, so a part can overshoot
the cap by at most that many rows (about 0.6 MB). Rows arrive ordered by
`TransactTime`, so dates never interleave and part numbering restarts at 1
per date. The aux rows are written to identically named files under
`aux/`, keeping the output directory itself upload-ready. A DQ failure in
strict mode removes every part written. `ExportResult.halo_paths` /
`aux_paths` list the files in order; `halo_path` / `aux_path` are the first
pair for callers that expect single files.

### 3.3 Symbol and SymbolType

`Symbol` is `TOKEN_A_SYMBOL/TOKEN_B_SYMBOL` for main-dex perps and spot,
and `TOKEN_A_SYMBOL-<PERP_DEX upper>/TOKEN_B_SYMBOL` for HIP-3 rows
(`TSLA-XYZ/USDC`, `1000PEPE-HYNA/USDE`), production's form; the dex is
embedded because HIP-3 market names are not unique across builder dexes,
and the quote token is the dex's collateral as Allium reports it. There
is no fallback for a NULL token symbol: such rows are gated out (§3.0),
or, with the gate off, ship with an empty `Symbol` and the raw ids in aux.

`SymbolType` is `Crypto` for spot and the seeded per-market value for
perps, NULL when the market is not in the map (no guessed fallback).
`hl_execs_halo_mapping.md` §2.3 and §2.4 have the details and the
regeneration command.

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
| HIP-3 `{dex}:{name}`  | `xyz:SP500`   | `SP500-XYZ/USDC`          | `SWAP`         |
| HIP-3, non-USDC dex   | `hyna:1000PEPE` | `1000PEPE-HYNA/USDE`    | `SWAP`         |
| `@N` spot index       | `@107`        | `HYPE/USDC` (resolved from `DEX.TRADES`; `@107/USDC` placeholder if no trade in the lookback) | `SPOT`         |
| `base/quote`          | `PURR/USDC`   | `PURR/USDC`               | `SPOT`         |

The HIP-3 form is the executions feed's / production's `TOKEN_A-DEX/TOKEN_B`
symbology (§3.3), so one contract has one HALO `Symbol` across both feeds.
The dex is kept in the name because HIP-3 market names are **not**
globally unique across builder dexes. `RAW.ORDERS` has no token symbols,
so the quote token comes from `orders_mapping.HIP3_DEX_QUOTE_TOKEN`
(dex prefix → collateral: USDC for xyz/io/para/mkts/abcd, USDE for hyna,
USDH for km/flx/vntl, USDT0 for cash, USDC fallback), rendered as a SQL
`CASE`. It is a hand-kept snapshot; see `hl_orders_halo_mapping.md` §8.2.

The orders feed also emits `Blockchain = 'ethereum'` and `SymbolType`
from the same map as executions (§3.3), so both feeds describe an
instrument identically.

`@N` spot indices are resolved by a `spot_pairs` CTE that groups
`DEX.TRADES` spot rows by `COIN` over `[start - spot_lookback_days, end)`
and LEFT JOINs the token symbols and `PAIR` onto the orders (same strings
the executions feed emits, so both feeds agree on `Symbol` and
`ExchangeSymbol`). The lookback (default 30 days,
`--spot-lookback-days`) bounds the scan; a pair with no trade in the
window keeps the `@N/USDC` placeholder, and the exporter counts those
rows and pairs (`OrdersExportResult.unresolved_spot_coins`) and prints a
warning. The raw `@N` stays in aux `_Coin`. See
`hl_orders_halo_mapping.md` §5.1.

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

### 5.3 SymbolType map is generated, not hand-edited

The map lives in production as `R__06b_symbol_type_map.sql` (itself
generated from the `hyperliquid_perps_universe.xlsx` workbook).
`hyperliquid_halo.sync_symbol_type_map` parses that SQL, validates it
(no duplicate coins, every value in the HALO `SymbolType` enum, no
characters that would break a SQL literal) and rewrites
`symbol_type_map.py`. Regenerate whenever production regenerates; the
mirror is committed so the exporter has no runtime dependency on the
production repo. Markets missing from the map ship with an empty
`SymbolType` in both places.

### 5.4 `Blockchain = 'ethereum'` on both feeds

HALO's supported Blockchain list has no Hyperliquid value. Both feeds
emit `ethereum` (HL accounts are EVM addresses), matching production.
The orders feed emitted the invalid `hyperliquid` until 2026-09-21.

## 6. Output file conventions

| File                | Schema                                                        | Join key (back to aux)       |
|---------------------|---------------------------------------------------------------|------------------------------|
| `halo.csv`, or `{prefix}_LINKED_PRIVATE_EXECUTION_V2_DDMMYYYY_partN.csv` when packaged | `mapping.HALO_COLUMNS`: v2.1 Execution Data in production column order (`TransactTime` .. `SymbolType`) + `IsMaker`; `mapping.HALO_STRICT_COLUMNS` (no `IsMaker`) under `--halo-strict` | `Id`                         |
| `aux.csv`, or `aux/<same part name>` when packaged | `mapping.AUX_COLUMNS`                              | (joined on `Id`)             |
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
validate-schema --csv output/<run>/halo.csv
validate-schema --csv output/<run>/halo_orders.csv
```

The executions exporter runs production's nine DQ checks itself on
every export (§3.5), so the SQL above is only needed when re-checking a
file after the fact.

## 8. Tooling

- **Tests** (`tests/`) use a `_FakeCursor` double — no Snowflake
  connection is opened. Both `mapping` / `orders_mapping` SQL assembly,
  both exporters' CSV writes, the DQ checker and the SymbolType map
  generator are covered. Run with `pytest` after `pip install -e ".[dev]"`.
- **VS Code launch configurations** (`.vscode/launch.json`) cover each
  CLI subcommand for quick debug runs against `.env`, an
  `--include-ineligible` export, and the SymbolType map sync (its
  `--source` path is relative to the workspace and assumes the
  production repo sits at `~/Desktop/defi-hyperliquid-halo`; edit it if
  yours lives elsewhere).
- **Snowflake auth** is environment-driven (`SNOWFLAKE_*`), supports
  both password and `SNOWFLAKE_AUTHENTICATOR=externalbrowser` SSO.
  See `snowflake_client.py` for the supported envar set.

## 9. Pointers

For field-level mapping detail and the decisions that drove individual
field choices:

- Executions: [`hl_execs_halo_mapping.md`](hl_execs_halo_mapping.md)
- Orders: [`hl_orders_halo_mapping.md`](hl_orders_halo_mapping.md) (see §8 decisions log)
