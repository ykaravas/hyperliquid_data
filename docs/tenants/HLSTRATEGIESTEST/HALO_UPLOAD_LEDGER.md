# HALO upload ledger (tenant HLSTRATEGIESTEST)

This file records which Hyperliquid execution dates have been shipped from this
repo to the Solidus HALO tenant `HLSTRATEGIESTEST`. It is separate from the
`HLRESEARCH` ledger on purpose: the two tenants receive their own copies of
the same source data, and what is on one says nothing about the other. Update
this file every time a window is exported, uploaded or verified. HALO cannot
de-duplicate hand uploads, so a part must never be sent twice; check this
ledger and the ClickHouse counts before re-sending anything.

## Tenant facts (as of 2026-10-20)

| Item | Value |
|---|---|
| Tenant name | `HLSTRATEGIESTEST` (`exchange` in `strict_events`, `solidus_client` in the raw tables, `--tenant` on the window commands) |
| Region | `uat-eu-central-1`, confirmed 2026-10-20 10:10 UTC by a read-only login through the `halo-upload` skill (`--tenant HLSTRATEGIESTEST --list-remote` answered `clientId=HLSTRATEGIESTEST`, 0 files on the tenant) |
| API key | `HALO_API_KEY_HLSTRATEGIESTEST` in `~/.claude/skills/halo-upload/.env` since 2026-10-20 (the skill requires `--tenant` on every call since the same day; there is no default tenant) |
| Part prefix | `hlstrategiestest` (Solidus's `<org>_<fileType>_<DDMMYYYY>_part<N>.csv` naming; `sdny` stays with HLRESEARCH, where the production pipeline's files use it) |
| Feeds | executions only so far (`LINKED_PRIVATE_EXECUTION_V2`, live route) |
| Output folder | `output/HLSTRATEGIESTEST/executions_20261010_20261019/` (the default `export-window` derives from `--tenant`) |
| State in ClickHouse (`uat-eu` / `solidus_uat_eu`) | no rows at all in `strict_events` or `raw_realtime_matched_executions` on 2026-10-20 10:30 UTC; `check-coverage` for Oct 10 to 19 returned every day empty |

## Coverage

| Transact dates (UTC) | Source | Parts | Rows in HALO | Uploaded (UTC) | Verified |
|---|---|---|---|---|---|
| 2026-10-10, sample only: `hlstrategiestest_LINKED_PRIVATE_EXECUTION_V2_10102026_part1.csv` (the first of the day's 8 parts, 830,000 rows, 499.1 MB) | this repo, `output/HLSTRATEGIESTEST/executions_20261010_20261019/` (`upload-window --only`, recorded in `upload_HLSTRATEGIESTEST.done`) | 1 of 139 | 830,000 expected | 2026-10-20 12:57:42 to 12:59:05 UTC, live route `LINKED_PRIVATE_EXECUTION_V2`, first attempt; the first file ever sent to this tenant, as the sample Solidus asks for before a full set | **REJECTED, 0 rows loaded.** HALO picked the file up at 12:59:59 UTC and finished it at 13:03:57 UTC (`processed` in `list-files`), but every one of the 830,000 rows went to `errors_view`: `DATA_VALIDATION`, field `transactTime`, "transactTime of 2026-10-10T... is more than a week before reference time of 2026-10-20T12:57:46Z" (the reference time is the upload request time). `raw_realtime_matched_executions` and `strict_events` hold 0 rows for the tenant. The rule is HALO's default backdated-data validation on the live route: `errors_view` shows the same message on more than 70 tenants, POLYMARKETRESEARCH and SDNYTEST among them, and never on HLRESEARCH, so HLRESEARCH has it disabled (which is why January data loaded there in September) and HLSTRATEGIESTEST does not. Options recorded in the Open items section below. **Re-sent 2026-10-20 13:48:57 to 13:50:16 UTC** (same file, first attempt, after the user changed the tenant's backdated-data setting; the rejected send's done-file line was removed first, backup `upload_HLSTRATEGIESTEST.done.rejected_20261020_1257`); verification pending. |

The other 138 parts (Oct 10 parts 2 to 8, Oct 11 to 19) are exported and
on hold. The sample's name sits in `upload_HLSTRATEGIESTEST.done` although
nothing loaded; remove that line before re-sending the part through
`upload-window`, otherwise the uploader skips it.

## Open items (2026-10-20)

The one-week rule blocks the live route for everything older than seven
days at upload time. Choices, for the user and Solidus to decide:

1. Ask Solidus to disable or widen the backdated `transactTime` validation
   on `HLSTRATEGIESTEST` as it is on `HLRESEARCH`, then re-send through the
   live route. Nothing loaded, so there is no duplicate risk; clear the
   sample from the done-file first.
2. Send through the historical route (`HISTORICAL_LINKED_PRIVATE_EXECUTION_V2`,
   `isHistorical = true`, the skill's `--historical`); files park under
   `historicalUploads/` until Solidus runs the tenant's historical ingest,
   which has to be requested. On HLRESEARCH in September the parked copies
   were never processed and had to be deleted.
3. Time-boxed: as of 2026-10-20 13:40 UTC, Oct 14 to 19 (88 parts, 44 GB)
   are still within the week and would pass the live route if sent before
   each day ages out (Oct 14 at 2026-10-21 00:00 UTC, Oct 15 a day later,
   and so on). Oct 10 to 13 need option 1 or 2 regardless.

## Exported, not uploaded

### 2026-10-10 to 2026-10-19, full market, executions (one sample part sent, see Coverage)

Exported 2026-10-20 from `ALLIUM_HYPERLIQUID.DEX.TRADES` with
`export-window --tenant HLSTRATEGIESTEST --start 2026-10-10 --end 2026-10-20`
(HALO-strict, 499 MB parts, production's eligibility gate, DQ-1 to DQ-9 per
day). The source was complete for every day when the export started: the
last trade of each day is at 23:59:59 UTC and Allium was up to date through
2026-10-20 10:27 UTC.

Source size per day (`DEX.TRADES` rows; each eligible trade becomes two HALO
rows, one per side):

| Date | Trades in `DEX.TRADES` | Markets |
|---|---|---|
| 2026-10-10 | 3,090,946 | 451 |
| 2026-10-11 | 3,899,396 | 433 |
| 2026-10-12 | 6,904,731 | 452 |
| 2026-10-13 | 6,577,496 | 446 |
| 2026-10-14 | 6,501,100 | 457 |
| 2026-10-15 | 8,648,232 | 473 |
| 2026-10-16 | 6,451,238 | 450 |
| 2026-10-17 | 3,599,724 | 443 |
| 2026-10-18 | 4,621,003 | 464 |
| 2026-10-19 | 5,803,551 | 447 |

**First pass, 2026-10-20 10:40 to 11:35 UTC (superseded, files overwritten).**
All ten days exported, 111,081,178 rows in 139 parts (95 GB with the aux
parts), DQ-1 to DQ-8 clean every day, but DQ-9 warned every day: the
exporter's mirror of the SymbolType map was the 2026-09-21 version (524
perps) and 827,556 rows (0.75%) on 11 newer HIP-3 markets shipped with an
empty `SymbolType`, mostly `PRL-IO/USDC` (about 630,000 rows) and
`PAPER-PARA/USDC` (189,638). The production repo's map
(`defi-hyperliquid-halo`, branch `yk_hl_orders_limited`, 2026-10-13) covers
10 of the 11 (`para:PAPER` is missing there too) and, since the same commit,
keys spot pairs by name with curated values instead of a constant `Crypto`
(`XAUT0/USDC` = Commodities, `USDT0/USDC` = Stablecoin, xStocks =
TokenizedEquity). Both changes were ported to the exporter (mirror
regenerated to 871 markets, spot joined by pair, DQ-9 extended to spot) and
the window was re-exported with `--force` (second pass 2026-10-20 11:47 to
12:43 UTC, 139 parts, 94 GB with the aux parts); the first-pass log is kept
in the output folder as `export_pass1_stale_symbol_map.log`. On the second
pass DQ-9 is clean on eight days and warns only on Oct 18 and 19 for
`PAPER-PARA/USDC` (152,618 and 37,020 rows), the one market production's map
lacks as well; those rows ship with an empty `SymbolType`, as production's
would. Spot values checked in the files: `PURR/USDC` Memecoin, `XAUT0/USDC`
Commodities, `USDT0/USDC` Stablecoin, `HYPE/USDC` Crypto. Note that
`origin/main` of the production repo, which is what the deployed daily
pipeline runs, did not have either change on 2026-10-20: `HLRESEARCH`'s
files for these days carry spot as `Crypto` and an empty `SymbolType` on
`GLW-XYZ`, `XIAOMI-IO`, `GDX-PARA`, `SMH-PARA` and `TWST-XYZ`.

Per-day targets (rows the exporter wrote on the second pass; compare
`strict_events` against them after the upload). Row counts are identical to
the first pass, since `SymbolType` is a column value and not a row filter:

| Date | Rows | Parts | DQ |
|---|---|---|---|
| 2026-10-10 | 6,077,972 | 8 | 9 of 9 |
| 2026-10-11 | 7,685,238 | 10 | 9 of 9 |
| 2026-10-12 | 13,702,756 | 17 | 9 of 9 |
| 2026-10-13 | 13,021,618 | 16 | 9 of 9 |
| 2026-10-14 | 12,922,196 | 16 | 9 of 9 |
| 2026-10-15 | 17,172,550 | 21 | 9 of 9 |
| 2026-10-16 | 12,787,630 | 16 | 9 of 9 |
| 2026-10-17 | 7,099,416 | 9 | 9 of 9 |
| 2026-10-18 | 9,150,908 | 12 | 8 of 9: DQ-9 WARN, 152,618 `PAPER-PARA/USDC` rows with empty `SymbolType` |
| 2026-10-19 | 11,460,894 | 14 | 8 of 9: DQ-9 WARN, 37,020 `PAPER-PARA/USDC` rows with empty `SymbolType` |
| **Total** | **111,081,178** | **139** | |

Comparison with `HLRESEARCH`, which holds the same ten days from the
deployed daily pipeline (same source, same eligibility gate), checked
2026-10-20 10:30 UTC in `strict_events`:

| Date | This export | `HLRESEARCH` rows | `HLRESEARCH` distinct ids | Difference |
|---|---|---|---|---|
| 2026-10-10 | 6,077,972 | 6,078,012 | 6,077,972 | equal on ids (40 duplicate rows there) |
| 2026-10-11 | 7,685,238 | 7,685,238 | 7,685,238 | equal |
| 2026-10-12 | 13,702,756 | 13,702,756 | 13,702,756 | equal |
| 2026-10-13 | 13,021,618 | 12,771,882 | 12,771,882 | `HLRESEARCH` short by 249,736 |
| 2026-10-14 | 12,922,196 | 12,767,629 | 12,767,629 | `HLRESEARCH` short by 154,567 |
| 2026-10-15 | 17,172,550 | 26,917,436 | 17,172,550 | equal on ids; `HLRESEARCH` loaded the day three times (Oct 15, 16 and a 14.6 M-row re-ship on Oct 17) |
| 2026-10-16 | 12,787,630 | 12,787,630 | 12,787,630 | equal |
| 2026-10-17 | 7,099,416 | 7,099,416 | 7,099,416 | equal |
| 2026-10-18 | 9,150,908 | 9,150,908 | 9,150,908 | equal |
| 2026-10-19 | 11,460,894 | 11,460,894 | 11,460,894 | equal |

Eight of ten days agree to the row. Oct 13 and 14 are larger here: the
daily pipeline ships each day in two runs (about 03:00 UTC on the day and
on the next day) and those two days were probably cut before Allium had
finished them; this export reads the complete days as Allium holds them on
2026-10-20. That is `HLRESEARCH`'s gap, not something to fix here.

## Before the first upload

1. The tenant's key is in the `halo-upload` skill's `.env`
   (`HALO_API_KEY_HLSTRATEGIESTEST`, added 2026-10-20) and logs in on
   `uat-eu-central-1`; the skill requires `--tenant` on every call.
2. `upload-window --tenant HLSTRATEGIESTEST --start 2026-10-10 --end 2026-10-20 --dry-run`
   and read the tenant, region, file type and part list it prints.
3. Send one part first (Solidus's own instruction, and the lesson of every
   earlier tenant): wait for `--list-remote` to show it `processed`, check
   the per-file count in `raw_realtime_matched_executions` and `errors_view`
   for `solidus_client = 'HLSTRATEGIESTEST'`, then send the rest.
4. Verify in ClickHouse per transact day against the per-day targets above
   and record the window in the Coverage table.

## How a window gets there

The procedure, the SQL and the lessons are in the HLRESEARCH ledger
([`../HLRESEARCH/HALO_UPLOAD_LEDGER.md`](../HLRESEARCH/HALO_UPLOAD_LEDGER.md),
"How a window gets there") and in the README ("Shipping a window to a
tenant"). Only the tenant changes:

```bash
hyperliquid-halo check-coverage --start 2026-10-10 --end 2026-10-20 --tenant HLSTRATEGIESTEST
hyperliquid-halo export-window  --start 2026-10-10 --end 2026-10-20 --tenant HLSTRATEGIESTEST
hyperliquid-halo upload-window  --start 2026-10-10 --end 2026-10-20 --tenant HLSTRATEGIESTEST --dry-run
```

Without `--out-dir` all three use
`output/HLSTRATEGIESTEST/executions_20261010_20261019/`; the done-file there
is `upload_HLSTRATEGIESTEST.done`.
