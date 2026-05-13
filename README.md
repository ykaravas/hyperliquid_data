# hyperliquid-halo

Schematize Allium's `ALLIUM_HYPERLIQUID.DEX.TRADES` Snowflake table into
Solidus **HALO Trade Surveillance v2.1** Execution Data CSVs, filtered by
date range and/or market (spot or perpetual).

Two CSV files are emitted per run:

| File       | Purpose                                                                                      |
|------------|----------------------------------------------------------------------------------------------|
| `halo.csv` | Strict HALO v2.1 upload file. Columns match the v2.1 Execution schema exactly.               |
| `aux.csv`  | Hyperliquid-specific supplementary columns (fees, PnL, TWAP IDs, liquidation, tx hash, etc.) joined to `halo.csv` on the `Id` column. |

See [`docs/FUNCTIONAL_SPEC.md`](docs/FUNCTIONAL_SPEC.md) for the full
field-by-field mapping rationale and the spot-vs-perpetual handling rules.

## Installation

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .           # exposes the `hyperliquid-halo` console script
```

Copy the Snowflake env template and fill in your credentials:

```bash
cp .env.example .env
$EDITOR .env
```

`SNOWFLAKE_AUTHENTICATOR=externalbrowser` is supported for SSO and avoids
having to put a password in `.env`.

## CLI

```bash
# Discover which markets are active in a date range
python -m hyperliquid_halo.cli list-markets \
    --start 2026-04-01 --end 2026-04-14

# Export BTC perps for two weeks
python -m hyperliquid_halo.cli export \
    --start 2026-04-01 --end 2026-04-14 \
    --coin BTC --market-type perpetuals \
    --out-dir ./data/btc_perps_202604

# Export a spot market (HYPE/USDC)
python -m hyperliquid_halo.cli export \
    --start 2026-04-01 --end 2026-04-14 \
    --token-a HYPE --token-b USDC --market-type spot \
    --out-dir ./data/hype_spot_202604

# Export ALL markets (both spot and perps, every coin)
python -m hyperliquid_halo.cli export \
    --start 2026-04-13 --end 2026-04-14 \
    --out-dir ./data/all_202604_13
```

### Filter options on `export`

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
- **Export: BTC perps (yesterday)** — one-day BTC perp export.
- **Export: HYPE spot (date range)** — two-week HYPE/USDC spot export.
- **List markets** — print the market summary for a range.

## Project layout

```
src/hyperliquid_halo/
    __init__.py
    mapping.py            # SQL template + QueryParams (buy/sell UNION)
    snowflake_client.py   # env-driven Snowflake connection helper
    exporter.py           # streams query results -> halo.csv + aux.csv
    cli.py                # click-based entrypoint
tests/
    test_mapping.py
    test_exporter.py
docs/
    FUNCTIONAL_SPEC.md    # per-field rationale + spot/perp handling
```
