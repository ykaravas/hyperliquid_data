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
| State in ClickHouse (`uat-eu` / `solidus_uat_eu`) | no rows at all in `strict_events` or `raw_realtime_matched_executions` on 2026-10-20 10:30 UTC (`check-coverage` for Oct 10 to 19 returned every day empty); after the first window, at 18:58 UTC the same day, 111,081,178 execution rows in the raw table and 110,977,279 in `strict_events` (Coverage) |

## Coverage

| Transact dates (UTC) | Source | Parts | Rows in HALO | Uploaded (UTC) | Verified |
|---|---|---|---|---|---|
| 2026-10-10, sample only: `hlstrategiestest_LINKED_PRIVATE_EXECUTION_V2_10102026_part1.csv` (the first of the day's 8 parts, 830,000 rows, 499.1 MB) | this repo, `output/HLSTRATEGIESTEST/executions_20261010_20261019/` (`upload-window --only`, recorded in `upload_HLSTRATEGIESTEST.done`) | 1 of 139 | 830,000 expected | 2026-10-20 12:57:42 to 12:59:05 UTC, live route `LINKED_PRIVATE_EXECUTION_V2`, first attempt; the first file ever sent to this tenant, as the sample Solidus asks for before a full set | **REJECTED, 0 rows loaded.** HALO picked the file up at 12:59:59 UTC and finished it at 13:03:57 UTC (`processed` in `list-files`), but every one of the 830,000 rows went to `errors_view`: `DATA_VALIDATION`, field `transactTime`, "transactTime of 2026-10-10T... is more than a week before reference time of 2026-10-20T12:57:46Z" (the reference time is the upload request time). `raw_realtime_matched_executions` and `strict_events` hold 0 rows for the tenant. The rule is HALO's default backdated-data validation on the live route: `errors_view` shows the same message on more than 70 tenants, POLYMARKETRESEARCH and SDNYTEST among them, and never on HLRESEARCH, so HLRESEARCH has it disabled (which is why January data loaded there in September) and HLSTRATEGIESTEST does not. Options recorded in the Open items section below. **Re-sent 2026-10-20 13:48:57 to 13:50:16 UTC** (same file, first attempt, after the user changed the tenant's backdated-data setting; the rejected send's done-file line was removed first, backup `upload_HLSTRATEGIESTEST.done.rejected_20261020_1257`). **Verified 2026-10-20 14:20 UTC: 830,000 of 830,000 rows loaded, ids distinct, 0 errors.** HALO parsed the file 13:51:15 to 13:54:14 UTC (about 4,600 rows/s), the raw table had all 830,000 rows (last row 829,999) at 14:06:21 UTC, `strict_events` at 14:18 UTC; 385 instruments; `SymbolType` stored as sent (Crypto 619,218, Equity 116,182, Memecoin 53,016, Commodities 40,390, Stablecoin 540, FX 316, Exotics 308, FixedIncome 28, TokenizedEquity 2; none empty). The rejected 12:57 copy stays `processed` in HALO's file list with 0 rows loaded, so it is harmless. |
| 2026-10-10 to 2026-10-19, full market, all ten transact days | this repo, `output/HLSTRATEGIESTEST/executions_20261010_20261019/` (`upload-window --start 2026-10-10 --end 2026-10-20 --tenant HLSTRATEGIESTEST --yes`; `upload_HLSTRATEGIESTEST.done` lists all 139 parts) | 139 (111,081,178 rows, 67.1 GB of HALO parts; the aux parts stayed local) | 111,081,178 expected; **110,977,279 loaded** | the sample 2026-10-20 13:48 UTC (row above), the other 138 parts 14:25 to 17:44 UTC, live route `LINKED_PRIVATE_EXECUTION_V2`, 3 workers, 0 failures, every part on its first attempt; HALO parsed the parts as they arrived and `list-files` showed all 140 entries `processed` at 18:57 UTC | **Verified 2026-10-20 18:58 UTC, one gap.** `raw_realtime_matched_executions` holds all 139 files complete: 111,081,178 rows, row numbers 0 to n-1 per file without gaps or duplicates, equal to the export ledger per day and per part; `errors_view` has nothing beyond the rejected 12:57 sample copy. `strict_events`: nine of ten days equal their targets with rows equal to distinct ids (Oct 10 to 16, 18 and 19); **Oct 17 holds 6,995,517 of 7,099,416 rows**. The 103,899 missing rows are all from `hlstrategiestest_LINKED_PRIVATE_EXECUTION_V2_17102026_part4.csv` (file rows 569,674 to 673,531 plus 41 rows between 569,318 and 569,669; transact times 12:59:50 to 13:18:59 UTC) and are present in the raw table, so this is HALO's raw-to-strict copy, not the upload; see "Open item" below. Empty `SymbolType` only on the `PAPER-PARA/USDC` rows (152,618 on Oct 18, 37,020 on Oct 19), as exported; 409 to 446 instruments per day. |

**Full send 2026-10-20 14:25 to 17:44 UTC** (user instruction after the
sample verified): `upload-window --start 2026-10-10 --end 2026-10-20
--tenant HLSTRATEGIESTEST --yes`, the remaining 138 parts, 3 workers, 3
attempts per part, live route, log in `upload.log` next to the parts; the
sample was skipped through the done-file. Result: 138 sent, 0 failed, every
part on its first attempt; `upload_HLSTRATEGIESTEST.done` lists all 139
parts. HALO's file list held 140 entries for the tenant (139 parts plus the
rejected 12:57 sample copy): 131 `processed` at 17:45 UTC, all 140 at 18:57
UTC. The raw table had every row by 18:14 UTC and the copy into
`strict_events` ran until 18:17 UTC. Verified 18:58 UTC: nine days complete,
Oct 17 short by 103,899 rows that sit in the raw table (next section but
one). The user closed the upload the same evening; no further send is
planned for this window.

## Resolved 2026-10-20: the one-week backdating rule

The live route rejects every row whose `transactTime` is more than seven
days before the upload request time (HALO's default; `HLRESEARCH` has it
disabled, which is why old data always loaded there). The first sample send
at 12:57 UTC lost all 830,000 rows to it. The user changed the tenant's
backdated-data setting in HALO at about 13:45 UTC, the same file re-sent at
13:48 UTC loaded in full, so `HLSTRATEGIESTEST` now accepts old data through
the live route like `HLRESEARCH`. The historical route and a time-boxed send
of the recent days were the alternatives considered and are not needed.

## Open item: Oct 17 part 4, 103,899 rows missing from `strict_events`

Found 2026-10-20 18:58 UTC, after the full send. The facts:

- `strict_events` holds 6,995,517 of the 7,099,416 Oct 17 rows; the other
  nine days are complete to the row, with distinct ids. The 103,899 missing
  rows are all from
  `hlstrategiestest_LINKED_PRIVATE_EXECUTION_V2_17102026_part4.csv`
  (829,000 rows): file rows 569,674 to 673,531 (103,858 rows, one block)
  plus 41 rows in 17 short runs between 569,318 and 569,669. The file is
  sorted by `TransactTime`, so in transact time the gap is 12:59:50 to
  13:18:59 UTC on Oct 17 (row 569,673, present, is 12:59:59.854; row
  569,674, missing, is 12:59:59.923; row 673,531, missing, is 13:18:59.867;
  row 673,532, present, is 13:19:00.007). The row numbers come from
  `SELECT number FROM numbers(829000) WHERE number NOT IN (SELECT orig_file_row_number FROM strict_events WHERE exchange = 'HLSTRATEGIESTEST' AND orig_file LIKE '%17102026_part4.csv%')`.
- The rows did reach HALO. `raw_realtime_matched_executions` holds all
  829,000 rows of the file (row numbers 0 to 828,999, no gaps, no
  duplicates), loaded by the S3 queue at 17:28:22 to 17:28:24 UTC from 64
  persistence objects, and `errors_view` has nothing for the file. The copy
  into `strict_events` (the market-data-builder copy job, which reads the
  raw table every two minutes) wrote the file's other 725,101 rows between
  18:06:58 and 18:17:09 UTC from the same 64 objects and left this one
  slice of transact times out of every object. The gap is therefore inside
  HALO's raw-to-strict copy, not in the upload, the parse or the file.
- Last copy activity for the tenant: 18:17:09 UTC. Nothing had changed by
  18:58 UTC.

How to close it, in order of preference:

1. Ask Solidus (data pipeline) to re-run the raw-to-strict copy for
   `HLSTRATEGIESTEST`, executions, `ts` between 2026-10-17 12:59:50 and
   13:19:00 UTC. The rows are already in their raw table, so no file has to
   move, and a window re-copy is the tool they use for duplicate clean-ups
   as well. Check first whether the nightly market-data-builder run has
   filled the gap on its own.
2. Only if Solidus cannot re-copy: build a supplement file holding exactly
   the 103,899 missing rows (filter part 4 by the row numbers above; the ids
   are unique, so no duplicate can arise), send it as a new file through the
   live route and record it here. Never re-send part 4 whole: HALO would
   load the 725,101 rows it already has a second time, which is how
   `HLRESEARCH`'s Oct 15 ended up with 9.7 M duplicate rows.

Until it is closed, Oct 17 on this tenant is short by 1.46% of its rows
(the 19 minutes from 12:59:50 UTC) and surveillance over that window runs
on incomplete data.

## Exported and uploaded

### 2026-10-10 to 2026-10-19, full market, executions (uploaded 2026-10-20, see Coverage)

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

Per-day targets (rows the exporter wrote on the second pass) and what
`strict_events` held after the upload. Row counts are identical to the first
pass, since `SymbolType` is a column value and not a row filter:

| Date | Rows | Parts | DQ | In `strict_events`, 2026-10-20 18:58 UTC |
|---|---|---|---|---|
| 2026-10-10 | 6,077,972 | 8 | 9 of 9 | 6,077,972 |
| 2026-10-11 | 7,685,238 | 10 | 9 of 9 | 7,685,238 |
| 2026-10-12 | 13,702,756 | 17 | 9 of 9 | 13,702,756 |
| 2026-10-13 | 13,021,618 | 16 | 9 of 9 | 13,021,618 |
| 2026-10-14 | 12,922,196 | 16 | 9 of 9 | 12,922,196 |
| 2026-10-15 | 17,172,550 | 21 | 9 of 9 | 17,172,550 |
| 2026-10-16 | 12,787,630 | 16 | 9 of 9 | 12,787,630 |
| 2026-10-17 | 7,099,416 | 9 | 9 of 9 | 6,995,517 (short 103,899, see Open item) |
| 2026-10-18 | 9,150,908 | 12 | 8 of 9: DQ-9 WARN, 152,618 `PAPER-PARA/USDC` rows with empty `SymbolType` | 9,150,908 |
| 2026-10-19 | 11,460,894 | 14 | 8 of 9: DQ-9 WARN, 37,020 `PAPER-PARA/USDC` rows with empty `SymbolType` | 11,460,894 |
| **Total** | **111,081,178** | **139** | | **110,977,279** |

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

## Before each upload (followed for the first window on 2026-10-20)

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
