# HALO tenants

One folder per tenant. Nothing that leaves this repo for HALO is shared
between tenants: each has its own output folder, its own part-name prefix,
its own upload ledger and its own API key (in the `halo-upload` skill's
`.env`, never here). The registry that the window commands read is
`src/hyperliquid_halo/tenants.py`; add a tenant there and create its folder
here when it first receives anything.

| Tenant | Also known as | Region | Part prefix | Feeds shipped | Ledger |
|---|---|---|---|---|---|
| `HLRESEARCH` | `UAT_HYPERLIQUID` | `uat-eu-central-1` | `sdny` (the production pipeline's daily files on this tenant use it) | executions (several 2026 windows), orders (xyz commodities, Feb 27 to Mar 11 2026) | [`HLRESEARCH/HALO_UPLOAD_LEDGER.md`](HLRESEARCH/HALO_UPLOAD_LEDGER.md) |
| `HLSTRATEGIESTEST` | | `uat-eu-central-1` (confirmed by a read-only login on 2026-10-20) | `hlstrategiestest` | executions, October 10 to 19 2026 (exported 2026-10-20, not yet uploaded) | [`HLSTRATEGIESTEST/HALO_UPLOAD_LEDGER.md`](HLSTRATEGIESTEST/HALO_UPLOAD_LEDGER.md) |

## What is kept apart, and where

| Per tenant | Location | Who keeps it |
|---|---|---|
| Exported windows (parts, aux files, `export_window.jsonl`, `upload_<TENANT>.done`) | `output/<TENANT>/<feed>_<first day>_<last day>/` (git-ignored) | `export-window` and `upload-window` derive this path from `--tenant` when `--out-dir` is not given |
| Part-name prefix | `<prefix>_<fileType>_<DDMMYYYY>_part<N>.csv` | `export-window` takes it from the registry when `--file-prefix` is not given |
| Upload ledger (what was sent, when, how it verified, what is still open) | `docs/tenants/<TENANT>/HALO_UPLOAD_LEDGER.md` | you, after every upload and verification |
| API key | `HALO_API_KEY_<TENANT>` in `~/.claude/skills/halo-upload/.env` | the skill; `upload-window` passes `--tenant` through |
| Rows on the tenant | ClickHouse `strict_events` (`exchange = '<TENANT>'`) and the raw tables (`solidus_client = '<TENANT>'`) | `check-coverage` reads them before every export |

The two tenants hold copies of the same source data (Allium's
`DEX.TRADES`), so the same transact date can legitimately exist on both.
The coverage check is always per tenant; a day on `HLRESEARCH` says nothing
about `HLSTRATEGIESTEST`.

## Adding a tenant

1. Register it in `src/hyperliquid_halo/tenants.py` (name, part prefix, a
   line on what it is for). A tenant not in the registry still works on the
   command line with generic defaults (prefix = name in lower case), but
   register it before the first upload so the prefix never changes.
2. Put its key in the `halo-upload` skill's `.env` as `HALO_API_KEY_<TENANT>`
   (and `HALO_REGION_<TENANT>` if it is not `uat-eu-central-1`).
3. Create `docs/tenants/<TENANT>/HALO_UPLOAD_LEDGER.md` from the
   `HLSTRATEGIESTEST` one and add a row to the table above.
4. Run `check-coverage --tenant <TENANT>` for the first window. On a brand
   new tenant every day comes back empty; that is also what you see when the
   tenant lives in a different ClickHouse environment (`--ch-env`), so
   confirm the region with whoever issued the key before trusting an empty
   result.
