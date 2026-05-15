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
| `halo_orders.csv` | `RAW.ORDERS`  | Strict HALO v2.1 Order Data upload file (one row per status-change event).                   |
| `aux_orders.csv`  | `RAW.ORDERS`  | Hyperliquid order extras (raw status / side / TIF, trigger details, TP/SL children, builder fees) joined to `halo_orders.csv` on `(Id, TransactTime)`. |

See the docs for the full field-by-field rationale and the pipeline
architecture:

- [`docs/hl_execs_halo_mapping.md`](docs/hl_execs_halo_mapping.md) — portable trades → HALO Execution mapping (field by field)
- [`docs/hl_orders_halo_mapping.md`](docs/hl_orders_halo_mapping.md) — portable orders → HALO Order mapping (field by field, plus §8 decisions log)
- [`docs/FUNCTIONAL_SPEC.md`](docs/FUNCTIONAL_SPEC.md) — repo-flavored architecture and algorithm spec (module layout, data flow, cross-cutting decisions)

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .           # exposes the `hyperliquid-halo` console script
```

Run `MfaAwsSSOtoken.sh` script in terminal to be able to access snowflake.

Copy the Snowflake env template and fill in your credentials:

```bash
cp .env.example .env
$EDITOR .env
```

`SNOWFLAKE_AUTHENTICATOR=externalbrowser` is supported for SSO and avoids
having to put a password in `.env`.

## CLI

Four subcommands — two for executions (`list-markets`, `export-execs`) and
two for orders (`list-order-coins`, `export-orders`).

### Executions (from `DEX.TRADES`)

```bash
# Discover which markets are active in a date range
python -m hyperliquid_halo.cli list-markets \
    --start 2026-04-01 --end 2026-04-14

# Export BTC perps for two weeks
python -m hyperliquid_halo.cli export-execs \
    --start 2026-04-01 --end 2026-04-14 \
    --coin BTC --market-type perpetuals \
    --out-dir ./data/btc_perps_202604

# Export a spot market (HYPE/USDC)
python -m hyperliquid_halo.cli export-execs \
    --start 2026-04-01 --end 2026-04-14 \
    --token-a HYPE --token-b USDC --market-type spot \
    --out-dir ./data/hype_spot_202604

# Export ALL markets (both spot and perps, every coin)
python -m hyperliquid_halo.cli export-execs \
    --start 2026-04-13 --end 2026-04-14 \
    --out-dir ./data/all_202604_13
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
| `--out-dir`      | Directory for `halo.csv` + `aux.csv` (default `./data`).                       |

Filters are ANDed. Passing *nothing* but a date range returns every trade
in the range.

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
    --out-dir ./data/btc_perps_202604

# Export all spot order activity for one day
python -m hyperliquid_halo.cli export-orders \
    --start 2026-04-13 --end 2026-04-14 \
    --market-type spot \
    --out-dir ./data/spot_orders_202604_13

# Export every order touching a specific user address
python -m hyperliquid_halo.cli export-orders \
    --start 2026-04-13 --end 2026-04-14 \
    --user 0xabc...def \
    --out-dir ./data/user_abc_202604_13
```

#### Filter options on `export-orders`

| Flag             | Meaning                                                                                       |
|------------------|-----------------------------------------------------------------------------------------------|
| `--start` *(req)* | Inclusive start date (UTC, `YYYY-MM-DD`) — applied to `ORDER_TIMESTAMP`.                     |
| `--end`   *(req)* | Exclusive end date (UTC, `YYYY-MM-DD`). Must be strictly greater than `--start`.             |
| `--coin`         | Allium `COIN` filter (`BTC`, `kPEPE`, `xyz:SP500`, `@107`, `PURR/USDC`).                      |
| `--market-type`  | `spot` (matches `@N`-prefixed and `base/quote` COINs) or `perpetuals` (everything else). Inferred from `COIN` shape — `RAW.ORDERS` has no native market-type column. |
| `--user`         | Filter by the on-chain `USER` address (useful for per-account analysis).                      |
| `--out-dir`      | Directory for `halo_orders.csv` + `aux_orders.csv` (default `./data`).                        |

`filled` order rows are filtered at source — fills are covered by the
trades pipeline and HALO order `Status` does not accept `Filled`. `Vault
Close` orders are also filtered (out of scope today). See §8 of
[`docs/hl_orders_halo_mapping.md`](docs/hl_orders_halo_mapping.md) for
the full list of decisions and deferred work.

## Validating output

The project integrates with Solidus's `validate-schema` skill:

```bash
validate-schema --csv data/btc_perps_202604/halo.csv
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

## Project layout

```
src/hyperliquid_halo/
    __init__.py
    mapping.py            # Executions: SQL template + QueryParams (buy/sell UNION)
    exporter.py           # Executions: streams query results -> halo.csv + aux.csv
    orders_mapping.py     # Orders: SQL template + OrdersQueryParams
    orders_exporter.py    # Orders: streams query results -> halo_orders.csv + aux_orders.csv
    snowflake_client.py   # env-driven Snowflake connection helper
    cli.py                # click-based entrypoint (4 subcommands)
tests/
    test_mapping.py
    test_exporter.py
    test_orders_mapping.py
    test_orders_exporter.py
docs/
    FUNCTIONAL_SPEC.md             # repo-flavored architecture + algorithms (both feeds)
    hl_execs_halo_mapping.md       # portable trades -> HALO Execution mapping (field by field)
    hl_orders_halo_mapping.md      # portable orders -> HALO Order mapping (field by field, + §8 decisions)
```
