# hyperliquid-halo

Schematize Allium's `ALLIUM_HYPERLIQUID` Snowflake datasets into Solidus
**HALO Trade Surveillance v2.1** CSVs, covering both **executions** (from
`DEX.TRADES`) and **orders** (from `RAW.ORDERS`), filtered by date range
and/or market (spot or perpetual).

Up to four CSV files are emitted, two per feed:

| File              | Source table  | Purpose                                                                                      |
|-------------------|---------------|----------------------------------------------------------------------------------------------|
| `halo.csv`        | `DEX.TRADES`  | Strict HALO v2.1 Execution Data upload file (one row per side, joined by `MatchingID`).      |
| `aux.csv`         | `DEX.TRADES`  | Hyperliquid execution extras (fees, closed PnL, TWAP IDs, liquidation, tx hash) joined to `halo.csv` on `Id`. |
| `halo_orders.csv` | `RAW.ORDERS`  | Strict HALO v2.1 Order Data upload file (one row per status-change event); under `--halo-strict` it is written as `sdny_PRIVATE_ORDER_V2_DDMMYYYY_partN.csv` parts instead. |
| `aux_orders.csv`  | `RAW.ORDERS`  | Hyperliquid order extras (raw status / side / TIF, trigger details, TP/SL children, builder fees) joined to `halo_orders.csv` on `(Id, TransactTime)`. |

Every executions export also runs production's nine data-quality checks
(DQ-1..DQ-9) over the written rows and prints the report; under
`--halo-strict` a failing check removes the files and exits non-zero,
exactly as production ships nothing when DQ fails.

The HALO files carry the same values the production Snowflake pipeline
(`defi-hyperliquid-halo`) ships: same eligibility gate, ids, symbology
(HIP-3 markets as `TSLA-XYZ/USDC`), `PositionEffect`, `Blockchain` and
per-market `SymbolType`. By default `halo.csv` keeps the raw `OrderID` (it matches
`RAW.ORDERS.ORDER_ID` directly) and adds a trailing `IsMaker` column;
`--halo-strict` switches both to production's form (`-B`/`-S` suffixed
order ids, no `IsMaker`) for files that go to HALO. The orders feed's `Id`
carries the same suffix, so strict-mode executions link to their orders
inside HALO. The parity table is in
`docs/hl_execs_halo_mapping.md` §5.

See the docs for the full field-by-field rationale and the pipeline
architecture:

- [`docs/hl_execs_halo_mapping.md`](docs/hl_execs_halo_mapping.md) — portable trades → HALO Execution mapping (field by field)
- [`docs/hl_orders_halo_mapping.md`](docs/hl_orders_halo_mapping.md) — portable orders → HALO Order mapping (field by field, plus §8 decisions log)
- [`docs/FUNCTIONAL_SPEC.md`](docs/FUNCTIONAL_SPEC.md) — repo-flavored architecture and algorithm spec (module layout, data flow, cross-cutting decisions)
- [`docs/solidus_batch_file_upload_instructions.pdf`](docs/solidus_batch_file_upload_instructions.pdf) — Solidus Help Center, "TS and TM - Batch File Upload Instructions (SFTP or API)", saved 2026-09-29: file types (`PRIVATE_ORDER_V2`, `LINKED_PRIVATE_EXECUTION_V2`), naming, the 500 MB cap, the upload API flow, batch timing

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .           # exposes the `hyperliquid-halo` console script
```

Copy the Snowflake env template and fill in your credentials:

```bash
cp .env.example .env
$EDITOR .env
```

### Snowflake authentication

The client resolves credentials in this order:

1. `SNOWFLAKE_PRIVATE_KEY_PATH` set: key-pair (JWT) auth, no password read.
2. `SNOWFLAKE_AUTHENTICATOR=externalbrowser`: Okta SSO in the browser, no password.
3. `SNOWFLAKE_PASSWORD`: plain password auth.

The shared service-user password stopped working on 2026-09-22, so every
connection runs as your own login (`SNOWFLAKE_USER=first.last@soliduslabs.com`).
Key-pair auth is the active, primary route: the author's public key was
registered on 2026-09-23, `.env` sets `SNOWFLAKE_PRIVATE_KEY_PATH`, and no
browser login happens. The client uses the key whenever
`SNOWFLAKE_PRIVATE_KEY_PATH` is set and then ignores both the authenticator
and the password. `SNOWFLAKE_ROLE` defaults to `DEV_READER`, the read-only
role, and every project that talks to Snowflake uses it.

Okta SSO (`SNOWFLAKE_AUTHENTICATOR=externalbrowser`) stays configured as the
fallback for anyone without a registered key, and is where a new user starts
until an admin registers their key. Each process opens the browser once; the
id token is cached in the macOS keychain because `requirements.txt` installs
the connector's `secure-local-storage` extra, so a loop of exports inside one
process logs in once.

Snowflake needs an **RSA** key (2048-bit or more) in **PKCS#8 PEM** form. Keys
from `ssh-keygen` do not qualify as generated (OpenSSH format, and Ed25519 is
not RSA), so make the pair with OpenSSL:

```bash
# private key (PKCS#8, unencrypted; add "-v2 aes256" instead of "-nocrypt" for a passphrase)
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -outform PEM -nocrypt -out ~/.ssh/snowflake_rsa_key.p8
chmod 600 ~/.ssh/snowflake_rsa_key.p8
# public key
openssl rsa -in ~/.ssh/snowflake_rsa_key.p8 -pubout -out ~/.ssh/snowflake_rsa_key.pub
# the value to hand to the Snowflake admin (header, footer and line breaks removed)
grep -v -- "-----" ~/.ssh/snowflake_rsa_key.pub | tr -d '\n'
```

A Snowflake admin (SECURITYADMIN or higher) registers the public key on the
user, which is the only step you cannot do yourself:

```sql
ALTER USER "FIRST.LAST@SOLIDUSLABS.COM" SET RSA_PUBLIC_KEY='MIIBIjANBg...';   -- the value from the last command
DESC USER "FIRST.LAST@SOLIDUSLABS.COM";  -- RSA_PUBLIC_KEY_FP shows the registered fingerprint
```

Then in `.env`:

```
SNOWFLAKE_PRIVATE_KEY_PATH=~/.ssh/snowflake_rsa_key.p8
SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=   # only for an encrypted key
```

If your own key is not registered yet, keep `SNOWFLAKE_PRIVATE_KEY_PATH`
commented out in `.env` and rely on the SSO fallback. A JWT error (rather than
"incorrect username or password") means the key path is being used but the key
is not registered yet.

## CLI

Seven subcommands: two for executions (`list-markets`, `export-execs`), two
for orders (`list-order-coins`, `export-orders`), and three for shipping a
window of days to a HALO tenant (`check-coverage`, `export-window`,
`upload-window`), described under
[Shipping a window to a tenant](#shipping-a-window-to-a-tenant).

### Executions (from `DEX.TRADES`)

```bash
# Discover which markets are active in a date range
python -m hyperliquid_halo.cli list-markets \
    --start 2026-04-01 --end 2026-04-14

# Export BTC perps for two weeks
python -m hyperliquid_halo.cli export-execs \
    --start 2026-04-01 --end 2026-04-14 \
    --coin BTC --market-type perpetuals \
    --out-dir ./output/btc_perps_202604

# Export a spot market (HYPE/USDC)
python -m hyperliquid_halo.cli export-execs \
    --start 2026-04-01 --end 2026-04-14 \
    --token-a HYPE --token-b USDC --market-type spot \
    --out-dir ./output/hype_spot_202604

# Export ALL markets (both spot and perps, every coin)
python -m hyperliquid_halo.cli export-execs \
    --start 2026-04-13 --end 2026-04-14 \
    --out-dir ./output/all_202604_13
```

#### Filter options on `export-execs`

| Flag             | Meaning                                                                        |
|------------------|--------------------------------------------------------------------------------|
| `--start` *(req)* | Inclusive start date (UTC, `YYYY-MM-DD`).                                     |
| `--end`   *(req)* | Exclusive end date (UTC, `YYYY-MM-DD`). Must be strictly greater than `--start`. |
| `--coin`         | Allium `COIN` filter (`BTC`, `ETH`, or a spot pair id like `@4`).              |
| `--market-type`  | `spot` or `perpetuals`. Omit for both.                                         |
| `--token-a`      | `TOKEN_A_SYMBOL` filter — base token (useful for spot).                        |
| `--token-b`      | `TOKEN_B_SYMBOL` filter — quote token (useful for spot).                       |
| `--out-dir`      | Directory for `halo.csv` + `aux.csv` (default `./output`).                       |
| `--include-ineligible` | Skip the production eligibility gate: also export liquidations, auto-deleveraging, vault aggregation rows, dust sweeps and trades with unresolved symbols. Research use; such a file is not what production ships to HALO. |
| `--halo-strict`  | Emit exactly what production ships: `OrderID`/`MatchingOrderID` carry the `-B`/`-S` side suffix, no `IsMaker` column, a failing DQ check removes the files and exits 1, and the output is packaged as per-date part files (below). Cannot be combined with `--include-ineligible`. |
| `--max-file-mb`  | Package the HALO output as per-transact-date part files capped at this size, named like production's `{prefix}_LINKED_PRIVATE_EXECUTION_V2_DDMMYYYY_partN.csv`, with the matching aux parts under `aux/`. Defaults to 250 (production's cap, well under HALO's 500 MB limit) with `--halo-strict`, otherwise single `halo.csv` + `aux.csv`. |
| `--file-prefix`  | Prefix for packaged part names (default `sdny`, production's tenant prefix). |

A typical upload-ready run, one day at a time so a DQ failure on one day
never discards the others:

```bash
python -m hyperliquid_halo.cli export-execs \
    --start 2026-01-12 --end 2026-01-13 --halo-strict \
    --out-dir ./output/halo_strict_202601
# -> output/halo_strict_202601/sdny_LINKED_PRIVATE_EXECUTION_V2_12012026_part1.csv, _part2.csv, ...
#    output/halo_strict_202601/aux/<same names>   (not for upload)
```

For a multi-day window use `export-window` instead (below): it runs that
one-day export per day, checks the tenant first and keeps a ledger.

Filters are ANDed. Passing *nothing* but a date range returns every
eligible trade in the range. By default the export applies production's
eligibility gate (see `docs/hl_execs_halo_mapping.md` §2.5), which drops
forced closures and unresolved symbols whole-trade.

### Orders (from `RAW.ORDERS`)

```bash
# Discover which COINs have order activity in a date range, with inferred
# market type and per-COIN event / unique-order / unique-user counts.
python -m hyperliquid_halo.cli list-order-coins \
    --start 2026-04-01 --end 2026-04-14

# Export BTC perp orders for one day
python -m hyperliquid_halo.cli export-orders \
    --start 2026-04-13 --end 2026-04-14 \
    --coin BTC --market-type perpetuals \
    --out-dir ./output/btc_perps_202604

# Export all spot order activity for one day
python -m hyperliquid_halo.cli export-orders \
    --start 2026-04-13 --end 2026-04-14 \
    --market-type spot \
    --out-dir ./output/spot_orders_202604_13

# Export every order touching a specific user address
python -m hyperliquid_halo.cli export-orders \
    --start 2026-04-13 --end 2026-04-14 \
    --user 0xabc...def \
    --out-dir ./output/user_abc_202604_13
```

#### Filter options on `export-orders`

| Flag             | Meaning                                                                                       |
|------------------|-----------------------------------------------------------------------------------------------|
| `--start` *(req)* | Inclusive start date (UTC, `YYYY-MM-DD`) — applied to `ORDER_TIMESTAMP`.                     |
| `--end`   *(req)* | Exclusive end date (UTC, `YYYY-MM-DD`). Must be strictly greater than `--start`.             |
| `--coin`         | Allium `COIN` filter (`BTC`, `kPEPE`, `xyz:SP500`, `@107`, `PURR/USDC`).                      |
| `--market-type`  | `spot` (matches `@N`-prefixed and `base/quote` COINs) or `perpetuals` (everything else; `#N` outcome markets are dropped at source). Inferred from `COIN` shape — `RAW.ORDERS` has no native market-type column. |
| `--user`         | Filter by the on-chain `USER` address (useful for per-account analysis).                      |
| `--out-dir`      | Directory for `halo_orders.csv` + `aux_orders.csv` (default `./output`).                        |
| `--spot-lookback-days` | Days before `--start` to scan `DEX.TRADES` when resolving `@N` spot pair ids to token symbols (`@107` → `HYPE/USDC`). Default 30. Pairs with no trade in the window keep an `@N/USDC` placeholder and are listed in a warning. |
| `--trigger-lookback-days` | Days before `--start` to scan `RAW.ORDERS` for the armed rows of trigger orders (default 14). A fired trigger order is re-stamped to its trigger time, so its later rows need the earlier armed rows for `StopPx`, `OrigTransactTime` and OCO pairing; an order armed before the lookback is reported as a plain Market / Limit order. |
| `--halo-strict` | Package the HALO rows as upload-ready parts named `sdny_PRIVATE_ORDER_V2_DDMMYYYY_partN.csv` (the export day), capped at `--max-file-mb` (default 499) each, with `--file-prefix` (default `sdny`). One day per run. `aux_orders.csv` stays one file. |
| `--exclude-post-only` | Drop post-only (`TIME_IN_FORCE = Alo`) rows, the market-maker quoting traffic. On 2026-03-02 that was 1.515 billion of 1.547 billion events (98%); without them the day is 31.3 million rows (about 20 GB of CSV) instead of about 1 TB. Gtc, Ioc, market and all trigger orders are kept. |
| `--position-lookback-days` | Days before `--start` to scan `DEX.TRADES` for the latest fill that gives a trader's position (default 30). Full-position TP/SL orders arrive with size 0, which HALO rejects; their `OrderQty` becomes the trader's position at placement (aux `_PositionSize`). |
| `--drop-zero-qty/--keep-zero-qty` | Rows whose `OrderQty` is still 0 after the position lookup are withheld from the HALO file by default (HALO rejects a zero quantity); they stay in `aux_orders.csv` and are counted either way. |

`filled` order rows are filtered at source — fills are covered by the
trades pipeline and HALO order `Status` does not accept `Filled`. `Vault
Close` orders and `#N` HIP-4 outcome-market rows are also filtered (out
of scope). `Id` is the Hyperliquid `oid` with the `-B`/`-S` side suffix
production's executions use for `OrderID`, and rows that share `Id` and
`TransactTime` (timestamps are block times) are ordered New, Replaced,
then Canceled/Rejected. See §8 of
[`docs/hl_orders_halo_mapping.md`](docs/hl_orders_halo_mapping.md) for
the full list of decisions and deferred work.

### Shipping a window to a tenant

Three subcommands turn a date range into files on a HALO tenant without
anything being hand-scripted per window. They are deterministic on purpose:
every step is recorded in a ledger file inside the output folder, re-running
a step skips what is already done, and a part that has been uploaded once is
never sent again.

```bash
# 1. What does the tenant already hold for these dates? (exit 2 if anything)
python -m hyperliquid_halo.cli check-coverage \
    --start 2026-05-17 --end 2026-05-22 --tenant HLRESEARCH

# 2. Export day by day (runs the same check first and refuses covered days)
python -m hyperliquid_halo.cli export-window \
    --start 2026-05-17 --end 2026-05-22 --tenant HLRESEARCH \
    --out-dir ./output/halo_strict_20260517_20260521

# 3. Upload, one skill call per part; safe to start while step 2 still runs
python -m hyperliquid_halo.cli upload-window \
    --start 2026-05-17 --end 2026-05-22 --tenant HLRESEARCH \
    --out-dir ./output/halo_strict_20260517_20260521
```

For a long window run steps 2 and 3 side by side under `nohup`; the uploader
polls the export ledger and sends each day as soon as it is marked ok.

| Command | What it does | Exit codes |
|---|---|---|
| `check-coverage` | Queries ClickHouse (`strict_events` and the raw executions table) through the `clickhouse-download` skill and prints rows, distinct ids and every source file per day. | 0 nothing on the tenant; 2 some day already holds rows; 1 query error |
| `export-window` | Runs the coverage check, then exports each day as its own `--halo-strict` query with 499 MB parts and appends one JSON line per day (`status`, `rows`, `parts`) to `export_window.jsonl`. Re-running skips days marked `ok` (`--force` re-exports them); stale parts of a day are deleted before it is exported again. | 0 all days ok; 1 a day failed (DQ or error); 2 coverage blocked |
| `upload-window` | Reads `export_window.jsonl`, sends every part of each `ok` day through the `halo-upload` skill (one call per file, exact name as the pattern, success = exit code 0, 3 attempts), and appends each success to `upload_<TENANT>.done` at once. Re-running sends only what that file does not list. Asks you to confirm the tenant unless `--yes`; `--dry-run` prints the plan. | 0 all sent; 1 a part failed or a day was skipped |

Options worth knowing on `export-window`: `--allow-covered YYYY-MM-DD`
(repeatable) exports a day the tenant already holds rows for, after you have
read the source-file listing and decided those rows are test data;
`--skip-coverage-check` skips the ClickHouse call entirely and is only for
when ClickHouse is down and the coverage is already known. On
`upload-window`: `--no-wait` skips days the ledger does not list instead of
polling for them; `--workers`, `--attempts`, `--retry-wait` and `--poll`
tune the run.

Two external dependencies, both listed in `.env.example`: the `halo-upload`
skill (`HALO_UPLOAD_SCRIPT`, default `~/.claude/skills/halo-upload/script.py`)
holds the per-tenant API keys in its own `.env`, and the `clickhouse-download`
skill (`CLICKHOUSE_DOWNLOAD_SCRIPT`, run with `CLICKHOUSE_DOWNLOAD_PYTHON`,
default `python3`, which must have `clickhouse-connect` installed) holds the
ClickHouse credentials. This repo never sees either set of secrets. Both
paths are machine-specific, which is a known portability hazard.

After the upload, verify the row counts in ClickHouse and record the window
in `docs/HALO_UPLOAD_LEDGER.md`; the procedure and the SQL are in that file.

## Keeping the SymbolType map in sync

Both feeds emit HALO `SymbolType` from `src/hyperliquid_halo/symbol_type_map.py`,
a generated mirror of production's per-market map
(`db/migrations/hyperliquid/R__06b_symbol_type_map.sql` in
`defi-hyperliquid-halo`). When production regenerates that map (new
listings), re-sync the mirror and commit it:

```bash
python -m hyperliquid_halo.sync_symbol_type_map \
    --source ../defi-hyperliquid-halo/db/migrations/hyperliquid/R__06b_symbol_type_map.sql
```

Perp markets missing from the map ship with an empty `SymbolType` (no
guessed fallback), the same as production.

## Upload ledger

`docs/HALO_UPLOAD_LEDGER.md` lists every transact date already shipped to the
HLRESEARCH tenant, the windows still missing, and the export, upload, and
ClickHouse verification steps. Check it before uploading; HALO cannot
de-duplicate a part sent twice. The ledger only knows about this repo's
uploads, so `export-window` also asks the tenant itself (`check-coverage`)
and refuses days that already hold rows.

## Validating output

The project integrates with Solidus's `validate-schema` skill:

```bash
validate-schema --csv output/btc_perps_202604/halo.csv
```

This runs the HALO v2.1 validator (required columns, enum values) against
the exported CSV.

## Running tests

```bash
pip install -e ".[dev]"
pytest
```

All tests run fully offline — the exporter tests monkeypatch the Snowflake
cursor with a fake double, so no credentials are needed in CI.

## VS Code

Pre-wired debug launch configurations live in `.vscode/launch.json`:

- **Python: Current File** — run the active file against `.env`.
- **Export: BTC perps (yesterday)** — one-day BTC perp execution export.
- **Export: HYPE spot (date range)** — two-week HYPE/USDC spot execution export.
- **List markets** — print the trade market summary for a range.
- **Export orders: BTC perps (yesterday)** — one-day BTC perp order export.
- **List order coins** — print the order activity summary by COIN.
- **Export: all markets incl. ineligible (one day)**: research export with the eligibility gate off.
- **Export: BTC perps (HALO strict, one day)**: production-shaped `halo.csv` (`--halo-strict`).
- **Sync SymbolType map from production**: regenerate `symbol_type_map.py`; assumes the production repo is at `~/Desktop/defi-hyperliquid-halo` (path is workspace-relative, edit if needed).

## Project layout

```
src/hyperliquid_halo/
    __init__.py
    mapping.py            # Executions: SQL template + QueryParams (buy/sell UNION)
    exporter.py           # Executions: streams query results -> halo.csv + aux.csv
    orders_mapping.py     # Orders: SQL template + OrdersQueryParams
    orders_exporter.py    # Orders: streams query results -> halo_orders.csv + aux_orders.csv
    symbol_type_map.py    # GENERATED per-market HALO SymbolType (mirror of production R__06b)
    sync_symbol_type_map.py  # regenerates symbol_type_map.py from the production SQL
    dq.py                 # Executions: streaming mirror of production's DQ-1..DQ-9
    snowflake_client.py   # env-driven Snowflake connection helper
    window.py             # per-day HALO-strict export of a window + export_window.jsonl ledger
    upload.py             # resume-safe upload of an exported window via the halo-upload skill
    coverage.py           # what a tenant already holds per day (ClickHouse via clickhouse-download)
    cli.py                # click-based entrypoint (7 subcommands)
tests/
    test_mapping.py
    test_exporter.py
    test_orders_mapping.py
    test_orders_exporter.py
    test_symbol_type_map.py
    test_dq.py
    test_snowflake_client.py
    test_window.py
    test_upload.py
    test_coverage.py
docs/
    FUNCTIONAL_SPEC.md             # repo-flavored architecture + algorithms (both feeds)
    HALO_UPLOAD_LEDGER.md          # transact dates already on HLRESEARCH, open housekeeping, procedure
    hl_execs_halo_mapping.md       # portable trades -> HALO Execution mapping (field by field)
    hl_orders_halo_mapping.md      # portable orders -> HALO Order mapping (field by field, + §8 decisions)
    solidus_batch_file_upload_instructions.pdf  # Solidus's own upload rules (file types, naming, 500 MB, API flow, batch timing)
```

## See also: the hyperliquid-investigator skill

The analyst-facing Hyperliquid knowledge (venue data model, Solidus table map, read-only query recipes, PnL rules, the public API client, case patterns) lives in the Claude Code skill at `~/.claude/skills/hyperliquid-investigator/`. The two mapping docs under `docs/` stay the canonical semantic references for the Allium to HALO V2.1 mapping (row expansion per side, PositionEffect, the 23-status collapse, ALO to PostOnly, brackets); the skill cites them and does not copy them.
