# HALO upload ledger (tenant HLRESEARCH)

This file records which Hyperliquid execution dates have been shipped from this
repo to the Solidus HALO tenant `HLRESEARCH` (also called `UAT_HYPERLIQUID`,
region `uat-eu-central-1`). Update it every time a window is uploaded or
verified. HALO cannot de-duplicate hand uploads, so a part must never be sent
twice; check this ledger and the ClickHouse counts before re-sending anything.

Since 2026-10-20 every tenant has its own ledger under `docs/tenants/<TENANT>/`
(index in [`../README.md`](../README.md)); this file moved here from
`docs/HALO_UPLOAD_LEDGER.md` unchanged and covers `HLRESEARCH` only. The
output folders named below predate the per-tenant layout
(`output/<TENANT>/<feed>_<first day>_<last day>/`) that `export-window` uses
by default now, and have since been deleted from disk.

## Coverage

| Transact dates (UTC) | Source | Parts | Rows in HALO | Uploaded (UTC) | Verified |
|---|---|---|---|---|---|
| 2026-01-12 to 2026-01-16 | this repo, `output/halo_strict_20260112_20260116/` | 71 | 28,839,276 | 2026-09-22 (live route; the first attempt via the historical route only parked copies) | 2026-09-22 17:10, every day at target, ids distinct |
| 2026-03-10 | this repo, `output/halo_strict_20260301_20260310/` (executions only) | 12 (5.5 GB) | 9,047,094 exported, DQ clean | 2026-09-29 21:12 to 21:25 UTC (live route, first attempt; parts 1 to 5 and 10 to 12 loaded 21:53 UTC, parts 6 to 9 were picked up by the hourly retry at 22:16 UTC and loaded 22:43 UTC) | 2026-09-30 15:51 UTC: 9,047,094 rows in `strict_events`, ids distinct. This upload was first written off on 2026-09-29, when only 8 parts had loaded and a deletion from the tenant was planned; on 2026-09-30 the decision was to leave the data where it is, so this day must never be re-sent |
| 2026-03-01 to 2026-03-09, markets `xyz:CL`, `xyz:BRENTOIL`, `xyz:NATGAS` only (symbols `CL-XYZ/USDC`, `BRENTOIL-XYZ/USDC`, `NATGAS-XYZ/USDC`) | this repo, `output/xyz_commodities_20260301_20260309/execs/` (`export-window --feed executions --coin xyz:CL --coin xyz:BRENTOIL --coin xyz:NATGAS`, ledger-backed) | 10 (1.7 GB) | 2,923,884 exported, DQ 9 of 9 every day (per day 22,600 / 110,684 / 159,362 / 112,502 / 140,442 / 370,356 / 387,608 / 376,826 / 1,243,504) | March 1 part sent 2026-09-30 15:28 UTC as a test (loaded fully by 15:38, see Orders); the other 9 parts 2026-09-30 17:07:56 to 17:12:45 UTC with `upload-window` (live route, 3 workers, every part on its first attempt, `list-files` showed all nine under `/queued/` or `/in_progress/` at 17:13 UTC) | 2026-09-30 17:53 UTC: March 1 at 100% (22,600). HALO started three files at once (March 2, 4, 5) and each one stalled after about 74,700 rows with the same signature as the orders file (2 s per 10,000 rows, then 6, 10, 21 s, then 160 s for the last 4,700 rows, then nothing): March 2 = 74,761 of 110,684 (67.5%), March 4 = 74,708 of 112,502 (66.4%), March 5 = 74,832 of 140,442 (53.3%), all still `/in_progress/`; March 3, 6, 7, 8, 9 (6 files) still `/queued/`, 0 rows. The stall was Solidus's pipeline change (rolled back about 18:20 UTC); after the rollback the queued files loaded and all ten March parts were re-sent at 19:21 UTC on the user's instruction (the loader skipped the rows already loaded, no duplicates). **Verified 2026-09-30 20:53 UTC: every day March 1 to 9 at exactly 100%, ids distinct** (Feb 27 and 28 likewise, see Orders). Whole-market rows for these days are NOT on the tenant (only these three markets), and the old `CL/USDC-SWAP` test rows for the same market remain alongside |
| 2026-03-11, markets `xyz:CL`, `xyz:BRENTOIL`, `xyz:NATGAS` only | this repo, `output/xyz_commodities_20260301_20260309/execs/` (`run_window_mar11.sh`: `export-window --feed executions`, then `upload-window`; the tenant held nothing for the day) | 1 (496.8 MB) | 804,508 exported, DQ 9 of 9 (= 2 x the eligible `DEX.TRADES` rows: xyz:CL 304,889, xyz:BRENTOIL 85,875, xyz:NATGAS 11,490 trades) | 2026-10-01 14:27:23 to 14:28:42 UTC (live route, first attempt; parsed 14:29:40 to 14:32:34, loaded 14:51:26 UTC) | 2026-10-01 14:57 UTC: 804,508 rows in the raw table and in `strict_events` (CL-XYZ/USDC-SWAP 609,778, BRENTOIL-XYZ/USDC-SWAP 171,750, NATGAS-XYZ/USDC-SWAP 22,980), ids distinct |
| 2026-04-20 to 2026-04-26 | this repo, `output/halo_strict_20260420_20260426/` (first window shipped with `export-window` / `upload-window`) | 65 (44 GB) | 50,963,506 exported, DQ clean every day; coverage check before export found nothing on the tenant for these days | 2026-09-27 23:17 to 2026-09-28 00:38 UTC (live route; 65 parts sent once each, no retries, 34 objects under `date=2026-09-27/` and 31 under `date=2026-09-28/`) | 2026-09-28 01:51 UTC, every day at target, ids distinct, one source file per part (65) and nothing else on these days; loading finished about 70 minutes after the last upload |
| 2026-04-27 to 2026-05-04 | this repo, `output/halo_strict_20260427_20260516/` | 75 | 58,611,202 | 2026-09-22 23:44 to 2026-09-23 00:30 | 2026-09-23 14:05, every day at target, ids distinct |
| 2026-05-05 to 2026-05-16 | this repo, `output/halo_strict_20260427_20260516/` | 135 (61 GB) | 106,556,140 exported, DQ clean every day | 2026-09-23 19:23 to 22:45 (live route; all 135 parts in S3 with matching sizes) | 2026-09-24 01:16, every day at target, ids distinct; whole Apr 27 to May 16 window = 165,167,342 rows, ids distinct |
| 2026-05-17 to 2026-05-21 | this repo, `output/halo_strict_20260517_20260521/` | 60 (27 GB) | 47,459,638 exported, DQ clean every day | 2026-09-24 21:47 to 23:32 (live route; all 60 parts in S3 once, sizes matching, no retries) | May 17 and 18 verified 2026-09-25 (this repo's rows exactly at target, ids distinct; only a few test rows pre-existed). May 19, 20 and 21 already held a copy each, so those days are double-counted until Solidus purges one copy (loading of this repo's files finished 2026-09-25 01:53); see Housekeeping |
| 2026-05-19 onward | Solidus production pipeline (`defi-hyperliquid-halo`), daily, not from this repo | n/a | n/a | May 19 loaded 2026-05-20, May 20 loaded 2026-05-21, intraday loads from 2026-05-21 04:00 UTC onward; all with the old id format `tradeid-B`/`-S` at first, and a 2026-08-28 backfill in the current format for rows the old gate missed; not May 22 as first assumed | owned by the pipeline |

Not uploaded by anyone as far as this repo knows: 2026-01-01 to 2026-01-11
and 2026-01-17 to 2026-04-19, except that **2026-03-01 to 2026-03-09 hold
test rows** that did not come from this repo (below).

**What the tenant holds for 2026-03-01 to 2026-03-09 (coverage check
2026-09-29).** One file, `date=2026-03-29/xyz_cl_halo_export_v2 .csv_20260329225745965`,
loaded 2026-03-29 23:06 to 23:33 UTC: 2,352,260 execution rows on the
single instrument `CL/USDC-SWAP` (the xyz:CL case export in the old
symbology, ids `TESTD-<n>-B/-S`, order ids `TD<n>`), 91,015 to 962,751
rows a day, plus five `CL/USDC` rows with `TEST-` ids and 35 rows from
four tiny test files on 2026-03-01 (`blockchain troubleshoot.csv`,
`halo_test_enums.csv`, `halo_test_swap_symbol.csv`,
`test_required_only.csv`). These are test uploads from the case work, not
a feed. Their ids and symbol differ from this repo's (`CL-XYZ/USDC`,
`<tradeid>-<coin>-<timestamp>-B/-S`), so an upload of the full market for
those days would not collide on id but would show the xyz:CL market twice
under two symbols. From this repo the tenant holds, as of 2026-09-30 15:51
UTC, all of 2026-03-10 (row above) and the two March 1 test parts recorded
under Orders. Run `check-coverage` for those days before any upload.

## Orders

| Order day (UTC) | Source | Parts | Rows | Uploaded (UTC) | Verified |
|---|---|---|---|---|---|
| 2026-03-01, markets `xyz:CL`, `xyz:BRENTOIL`, `xyz:NATGAS` only, post-only included | this repo, `output/xyz_commodities_20260301_20260309/orders/` (`export-window --feed orders --include-post-only`, ledger-backed) | 1 of the window's 66 (`sdny_PRIVATE_ORDER_V2_01032026_part1.csv`, 268 MB) | 706,145 (27 unsized full-position stops withheld) | 2026-09-30 13:22 UTC, live route `PRIVATE_ORDER_V2`, first attempt; a test of the orders feed on this tenant before the other 65 parts | 2026-09-30 15:51 UTC: NOT fully loaded. HALO's file processor read the first 89,485 rows (0 to 89,484) between 14:14:51 and 14:17:35 UTC, slowing from about 3 s per 10,000 rows to 118 s for the last 9,485 and then stopping; an hourly retry at 15:16 UTC resumed at row 89,485, processed 238 rows (to 89,722) in 22 s and stopped again. 89,723 rows are in `strict_events` (`event_type = 'ORDER'`, last `ts` 2026-03-01 05:12:09 UTC), 0 rows rejected (`errors_view` has nothing for the tenant), `Notional` accepted on every row, `StopPx` on the 121 stop rows; `list-files` shows the file under `/in_progress/`. The later stages are fast (Kafka arrival 0 s after the parse, normalization 0.2 to 0.6 s), so the stall is inside the file processor itself. Waiting for the hourly retries or for Solidus. **Re-sent 2026-09-30 16:14:17 to 16:15:05 UTC** (same file, same bytes, md5 `2d85553c20afbe355aa481b5219e0b10`, live route, on the user's instruction after Solidus suggested the interruption was a system update); the new object is `date=2026-09-30/sdny_PRIVATE_ORDER_V2_01032026_part1.csv_20260930161418029` (`list-files`: `/queued/` at 16:14:18 UTC) while the first copy was still `/in_progress/` and was picked up again by the hourly retry at 16:14:38 UTC. The user accepted a possible duplication of the 89,723 rows the first copy had loaded; in the end none occurred (below). **Third send 2026-09-30 18:36:52 to 18:37:33 UTC** (same bytes) after Solidus rolled the upload pipeline back at about 18:20 UTC; at that moment `list-files` showed both earlier copies back in `/in_progress/` (18:19:48) and the stalled execution files re-queued, so the rolled-back pipeline is re-processing everything on its own as well. **The third copy loaded normally:** picked up 18:38:29 UTC, parsed at about 5,500 rows/s (last row reached 18:40:17), 616,422 rows in the raw table by 19:08 UTC, and those are exactly rows 89,723 to 706,144: HALO skipped the 89,723 rows the first copy had already loaded, so the tenant holds the file once (706,145 raw rows across the two copies, no duplicates; the second copy, `..._161418029`, was never loaded). Verified 2026-09-30 19:20 UTC: every row of the file is in `strict_events`; a per-day count shows 703,557 only because 2,645 rows of this March 1 file carry a `TransactTime` on March 2 or later (status changes of orders placed on March 1; the latest in July 2026), which is how the orders feed is windowed. Conclusion: the stall was Solidus's pipeline change, and the orders feed loads fine on the rolled-back pipeline |

**Executions test on the same folder, 2026-09-30 15:28 UTC:** `execs/sdny_LINKED_PRIVATE_EXECUTION_V2_01032026_part1.csv` (March 1, the three markets, 22,600 rows, 13.9 MB, DQ 9 of 9) sent through the live route `LINKED_PRIVATE_EXECUTION_V2`, first attempt, recorded in `execs/upload_HLRESEARCH.done`. Sent to see whether an executions file triggers processing while the orders file of 13:22 UTC had not been picked up after two hours. Verified 2026-09-30 15:40 UTC: processed within 24 s of the upload (15:28:44 to 15:28:52 UTC), loaded 15:37:58 UTC, all 22,600 rows in `strict_events` (`CL-XYZ/USDC-SWAP` 17,248, `NATGAS-XYZ/USDC-SWAP` 5,352; the file has no BRENTOIL fills), `list-files` shows it under `/processed/`. So the live route and the executions feed are fine; the problem is specific to the orders file.

The other 9 execution parts of the folder (March 2 to 9, 2,901,284 rows) were sent on 2026-09-30 17:08 to 17:13 UTC (Coverage table above), on the user's decision to go ahead with executions while orders stay on hold. Still pending on the same folder: the other 65 order parts (March 2 to 9, 80,159,507 rows), exported and validated on 2026-09-30, not to be sent until the orders stall is understood and the user says so.

**2026-09-30 19:21 to 19:32 UTC: the full re-send, executions side** (user decision: send everything from Feb 28 to March 9 again, both feeds, and let HALO's loader skip what it already holds). The executions done-file was reset to Feb 27 only (backup `upload_HLRESEARCH.done.before_resend_20260930`) and `upload-window --feed executions --start 2026-02-28 --end 2026-03-10` sent 11 parts (Feb 28 plus all ten March parts again, 1.7 GB) between 19:21:23 and 19:31:38 UTC, every part on its first attempt. The orders side (`--feed orders`, same window, 66 parts, 29 GB, `sdny_PRIVATE_ORDER_V2_01032026_part1.csv` skipped by the done-file) started 19:21 UTC; see below when it finishes. Executions verified 2026-09-30 20:17 UTC: Feb 27, Feb 28, March 1, 2, 3, 5, 6, 7, 8 at exactly 100% with ids equal to rows, so the re-sent copies added only the rows the stalled copies had missed (March 2: 105,394 + 5,290 = 110,684; March 6: 172,965 + 197,391 = 370,356) and nothing was duplicated. March 4 and March 9 completed by 20:53 UTC (112,502 and 1,243,504, ids distinct), so **all eleven days of executions, Feb 27 to March 9, are at exactly 100% for the three markets**. **Orders side finished 20:44:53 UTC:** 66 parts, 80,796,239 rows (Feb 28 = 1 part, March 2 = 4, March 3 = 6, March 4 = 6, March 5 = 8, March 6 = 9, March 7 = 3, March 8 = 3, March 9 = 26), every part on its first attempt, 0 failed; with Feb 27 (2 parts) and March 1 part 1 the orders done-file now lists all 69 parts of the folder. So as of 2026-09-30 20:45 UTC everything in `output/xyz_commodities_20260301_20260309/` (Feb 27 to March 9, both feeds) has been sent once; loading of the 29 GB of orders took about 90 minutes. **Orders verified 2026-09-30 22:05 UTC** (rows per uploaded object in `raw_realtime_orders` against the export ledger): Feb 27, Feb 28, March 1, 2, 3, 4, 5, 6, 7 and 8 at exactly 100% (1,711,377 / 636,732 / 706,145 / 5,144,186 / 7,453,019 / 6,881,300 / 9,501,894 / 11,110,147 / 2,811,764 / 3,639,392); March 9 at 30,014,000 of 33,617,805 (24 of 26 objects loaded, the last two still in HALO's queue). Executions re-checked at the same time: all eleven days at 100%.

**2026-09-30 about 22:15 UTC: the user deleted every execution row of March 2 to 9 from the tenant** (through HALO's tooling, on the belief that CL rows had been marked as duplicates of older ones; the ClickHouse counts had shown every day at exactly 100% with distinct ids, so no duplicate drop was visible from here). Checked 22:22 UTC: March 2 to 9 hold 0 execution rows in `strict_events` and in `raw_realtime_matched_executions` for every instrument, including the old `CL/USDC-SWAP` test rows (which remain on March 1 only, 17,278 rows); March 1 and March 10 untouched. On the user's instruction the nine March 2 to 9 execution parts were sent a third time starting 22:2x UTC (`upload-window --feed executions --start 2026-03-02 --end 2026-03-10`, done-file reset to Feb 27, Feb 28 and March 1; backup `upload_HLRESEARCH.done.before_resend2_20260930`). The nine parts went up 22:23 to 22:33 UTC, first attempt each. **Verified 23:00 UTC: the re-send loaded in full.** Every one of the nine objects was parsed within about 15 seconds of its PUT at full speed (March 9 part 1: 810,000 rows in 2 min 53 s), and `strict_events` has every day March 2 to 9 at exactly 100% with distinct ids again. So the loader's duplicate check did not block rows that had been deleted from the tenant, and the same should hold for the orders re-send.

**Found 22:29 UTC: the deletion removed the March 2 to 9 ORDERS as well.** It was evidently a delete by event date across both feeds: `raw_realtime_orders` keeps only the rows of the March 2 to 9 files whose `TransactTime` falls outside those dates (March 2 = 71 rows, March 3 = 22, March 4 = 26, March 5 = 22, March 6 = 77, March 7 = 170, March 8 = 402, March 9 = 8,932, all with later status-change times), `strict_events` has no order rows at all for March 2 to 9, and Feb 27, Feb 28 and March 1 each lost the few rows whose status change fell in March 2 to 9 (1,711,353 / 636,456 / 703,583 raw rows remain). So 80.2 million order rows (the 65 March 2 to 9 parts) are gone from the tenant and would have to be sent again; the user was told and asked to decide. `sdny_PRIVATE_ORDER_V2_09032026_part8.csv` itself had uploaded fine (20:24:07 UTC, first attempt, `processed` in HALO, its rows were in the raw table at 22:05) before the deletion took it with the rest. **Re-send of the 65 March 2 to 9 order parts, 2026-09-30 22:52:44 to 2026-10-01 00:38:27 UTC, every part on its first attempt, 0 failed** (March 2 = 4, March 3 = 6, March 4 = 6, March 5 = 8, March 6 = 9, March 7 = 3, March 8 = 3, March 9 = 26), on the user's instruction (`upload-window --feed orders --start 2026-03-02 --end 2026-03-10`; orders done-file reset to Feb 27, Feb 28 and March 1, backup `upload_HLRESEARCH.done.before_resend_20260930`; log `orders/upload_resend_20260930.log`). Found 23:01 UTC: three March 9 parts of the FIRST orders send (`part8`, `part10`, `part17`, upload stamps 20:20 to 20:31 UTC, the ones HALO had left queued) were parsed by HALO's sweep at 22:14 to 22:18 UTC and loaded at 22:42, i.e. after the deletion, so 3,964,000 March 9 rows from the first send are back on the tenant next to the re-send. If the loader skips rows already present (as it did at 18:38), the re-sent copies of those three parts add nothing; if not, March 9 will exceed its 33,617,805 target by that amount and the three first-send objects would have to be deleted by file. Check the per-day count after the load. Interim 2026-10-01 00:36 UTC (re-send at 62 of 65 parts): March 2 and 3 at exactly their targets although the leftover sliver objects are still present, so the loader is skipping rows the tenant already holds; March 4 to 10 still parsing. Feb 27, Feb 28 and March 1 remain short by the rows the deletion took (24, 276 and 2,562 rows with status changes dated March 2 to 9); re-sending those four parts would restore exactly those rows, since the loader adds only what is missing. March 10 orders for the three markets were exported into the same folder at the same time (`run_window_mar10.sh`, 22:44 to 22:53 UTC, `--allow-covered 2026-03-10` because the leftover post-March-9 status rows made the gate see the day as covered): **28,456,874 rows in 22 parts** plus `aux_orders_10032026.csv`, 25,457 full-position stops sized from position, 4,025 withheld at OrderQty 0. **Sent starting 2026-09-30 23:05 UTC** on the user's instruction (`upload-window --feed orders --start 2026-03-10 --end 2026-03-11`, 22 parts, 10.8 GB, log `orders/upload_mar10_20260930.log`), running next to the March 2 to 9 re-send on the same done-file; **all 22 parts up by 23:58:00 UTC, first attempt each, 0 failed**. Tenant-side note: March 10 already holds the full-market executions of that day, and the ORDER rows dated March 10 before this send were only the post-March-9 status changes of orders placed earlier. Verification pending (target 28,456,874).

**Final orders verification, 2026-10-01 14:35 UTC** (`raw_realtime_orders`, rows per order day over both upload-date folders `date=2026-09-30/` and `date=2026-10-01/`; the parts sent after midnight UTC sit in the second folder, which is why a first count filtered on the 30th showed March 8 at 402 rows and March 9 at 3.97 million): **March 2 to 10 are at exactly their targets** (5,144,186 / 7,453,019 / 6,881,300 / 9,501,894 / 11,110,147 / 2,811,764 / 3,639,392 / 33,617,805 / 28,456,874). March 9 is exact although its 27 loaded objects include the three first-send parts (part8, part10, part17) next to the 26 re-sent ones, so the loader skipped the re-sent copies of rows it already held; nothing has to be deleted by file. Feb 27, Feb 28 and March 1 stand at 1,711,353 / 636,456 / 703,583 (short by the 24 / 276 / 2,562 rows the deletion took; re-sending those four parts would restore exactly those rows, not done). HALO `list-files` had shown all 158 order objects `processed` by 03:21 UTC. The session's queries from 03:20 to 03:37 UTC had failed on the ClickHouse side (connection reset), which is why this check came the next morning.

**2026-10-01 14:26 to 14:28 UTC: March 11, 2026 added to the same folder and sent, executions side** (user instruction: schematize both feeds for the three markets, then upload; `run_window_mar11.sh`). `check-coverage` first: HLRESEARCH held nothing for March 11 executions; for orders it showed 5,997 rows, all late status changes from the earlier files, so the orders export runs with `--allow-covered 2026-03-11`. Executions: `sdny_LINKED_PRIVATE_EXECUTION_V2_11032026_part1.csv`, **804,508 rows in 1 part (496.8 MB), DQ 9 of 9**, exported 14:26:23 to 14:27:23 UTC, sent 14:27:23 to 14:28:42 UTC on the first attempt (`execs/upload_mar11_20261001.log`), recorded in the execs done-file (13 lines now). Orders export started 14:28:42 UTC; see below.

**March 11 executions verified 2026-10-01 14:53 UTC:** HALO parsed the file 14:29:40 to 14:32:34 UTC (about 4,600 rows/s) and loaded it 14:51:26 UTC; `raw_realtime_matched_executions` holds 804,508 rows, 804,508 distinct ids, last row 804,507. Snowflake cross-check: the exporter's strict query counts 804,508 rows / 804,508 ids for the day, equal to 2 x the eligible `DEX.TRADES` rows (xyz:CL 304,889 trades, xyz:BRENTOIL 85,875, xyz:NATGAS 11,490; no liquidations, ADL, vault or token-less rows), so the file is the whole day for the three markets. `strict_events` confirmed 14:57 UTC: CL 609,778 / BRENTOIL 171,750 / NATGAS 22,980 rows, ids distinct (Coverage table).

**March 11 orders, exported 14:28:42 to 14:36:33 UTC and sent from 14:36:33 UTC:** `sdny_PRIVATE_ORDER_V2_11032026_part1.csv` to `_part19.csv` plus `aux_orders_11032026.csv`, **24,109,924 rows in 19 parts (9.1 GB)**, 21,967 full-position stops sized from position, 226 withheld at OrderQty 0, validator clean (12,886,062 orders, 7,963 accounts; symbols CL-XYZ/USDC 16,768,186 / BRENTOIL-XYZ/USDC 6,547,263 / NATGAS-XYZ/USDC 794,475; Status New 11,647,785 / Canceled 11,220,934 / Rejected 1,238,313 / Replaced 2,892; TrdType PostOnly 20,811,421 / RegularTrade 3,298,503; OCO 12,780). `upload-window --feed orders --start 2026-03-11 --end 2026-03-12` (log `orders/upload_mar11_20261001.log`) sends the 19 parts through the live route; HALO was parsing them as they arrived (parts 1 to 4 and 6 `processed` by 14:50 UTC). **All 19 parts sent by 14:59:51 UTC, first attempt each, 0 failed** (orders done-file now 110 lines). Per-file verification against 24,109,924 (query in the skill's Verification section, filter on the file name, not the `date=` folder) is left to the user, who took over monitoring at 15:00 UTC.

**2026-10-01 17:43 to 18:41 UTC: March 10 and March 12 to 20 exported into the same folder, NOT uploaded** (user instruction: download and schematize both feeds for the three markets; `run_window_execs_mar10_20.sh` and `run_window_orders_mar12_20.sh`). Executions (`--allow-covered` for March 10, which the tenant holds in full from the full-market upload, and for March 11, our own upload; March 11 skipped by the export ledger; March 12 to 20 empty on the tenant): March 10 = 912,982 rows (2 parts), March 12 = 809,736 (2), March 13 = 573,844 (1), March 14 = 532,626 (1), March 15 = 526,404 (1), March 16 = 604,394 (1), March 17 = 404,140 (1), March 18 = 662,482 (1), March 19 = 942,778 (2), March 20 = 602,430 (1); 6,571,816 rows, 13 parts, DQ 9 of 9 every day. **The March 10 three-market file is a duplicate of rows already on HLRESEARCH under the same symbols (`CL-XYZ/USDC-SWAP` and so on, from the full-market upload of 2026-09-29) and must not be uploaded.** Orders (`--allow-covered` on every day; the tenant's 123 to 1,185 ORDER rows per day there are late status changes from the March 1 to 11 files): March 12 = 26,328,790 rows (20 parts; 29,738 stops sized from position, 456 withheld at OrderQty 0), March 13 = 18,828,784 (15; 16,737 / 221), March 14 = 5,390,656 (5; 16,945 / 246), March 15 = 9,004,569 (7; 16,039 / 1,472), March 16 = 16,597,810 (13; 15,450 / 280), March 17 = 13,632,260 (11; 8,800 / 125), March 18 = 19,374,181 (15; 15,964 / 270), March 19 = 28,216,864 (22; 17,252 / 278), March 20 = 19,377,330 (15; 13,150 / 227); 156,751,244 rows in 123 parts plus one aux file per day, 4 to 9 minutes a day. The folder now holds executions Feb 27 to March 20 (26 parts, 9.0 GB; 13 of them sent) and orders Feb 27 to March 20 (233 parts, 22 aux files, 170 GB; 110 of them sent). Local validator over the nine new orders days (18:45 to 19:09 UTC): clean on every day, no problems; rows and withheld counts equal to the export log's, every part at most 499 MB, symbols CL-XYZ/USDC / BRENTOIL-XYZ/USDC / NATGAS-XYZ/USDC only. Shape per day: 3.2 to 16.1 million orders from 4,528 to 9,305 accounts; post-only 41% (March 14) to 88% (March 12 to 13, 17, 20) of the rows; Rejected 2% (March 20) to 28% (March 15) of the rows; Replaced always below 5,000.

**2026-09-30 18:57 to 18:59 UTC: Feb 27 sent, both feeds** (user decision after the rollback; first live day of the re-upload): `execs/sdny_LINKED_PRIVATE_EXECUTION_V2_27022026_part1.csv` (30,076 rows, 18.5 MB) at 18:57:56 UTC and `orders/sdny_PRIVATE_ORDER_V2_27022026_part1.csv` + `_part2.csv` (1,711,377 rows, 499.3 MB + 142.8 MB) at 18:58:35 and 18:59:36 UTC, all on the first attempt through `upload-window`, recorded in both done-files. Verified 2026-09-30 19:26 UTC: executions 30,076 of 30,076 in the raw table and in `strict_events` (ids distinct); orders 1,711,377 of 1,711,377 in `raw_realtime_orders` (876,785 distinct ids, as in the file), all three files `processed` by 19:05 UTC. A day-bounded `strict_events` count shows 1,693,285 order rows because the rest carry a later `TransactTime` (see the March 1 note).

**2026-09-30 18:26 to 18:31 UTC: Feb 27 and 28, 2026 added to the same folder** (user decision, same three markets, post-only included, `run_window_feb.sh`; HLRESEARCH held nothing for either day). Executions: Feb 27 = 30,076 rows (1 part, 17.6 MB), Feb 28 = 41,814 rows (1 part, 24.8 MB), DQ 9 of 9 both days, both equal to 2 x the eligible DEX.TRADES rows. Orders: Feb 27 = 1,711,377 rows (2 parts, 612 MB, 416 stops sized from position, 10 withheld at OrderQty 0, 268 OCO), Feb 28 = 636,732 rows (1 part, 230 MB, 1,024 sized, 41 withheld, 732 OCO), one aux file per day, validator clean. Nothing of it uploaded. Solidus said the same day that the upload pipeline is broken and being reverted, so the plan is to re-upload the whole folder (Feb 27 to Mar 9, both feeds) once they confirm; the parts already sent on 2026-09-30 (10 execution parts, 1 order part twice) will then have to be treated as duplicates or purged by Solidus.

### Earlier notes (before the first orders upload)

The orders feed has not been sent to any tenant. Decided 2026-09-29: the
tenant for orders is `HLRESEARCH` too, file type `PRIVATE_ORDER_V2` through
the live route, and nothing goes up until three things are settled: the
window commands learn the orders feed (part names, file type, coverage
against `event_type = 'ORDER'`, a separate done-file), and Solidus answers
two questions recorded in `hl_orders_halo_mapping.md` §8.2 (#8, whether
`OrderQty = 0` on full-position stops is accepted; #12, whether HALO wants
the post-only quoting traffic at all). Since 2026-09-29 `export-orders`
leaves the post-only quoting out by default (`--include-post-only` to
keep it), so every orders export listed here is without it unless the row
says otherwise.

| Order day (UTC) | Source | Parts | Rows | Status |
|---|---|---|---|---|
| 2026-03-02 | this repo, `output/orders_20260302/` (`export-orders --halo-strict --exclude-post-only`, regenerated 2026-09-29 15:34 UTC with `Notional` and position-sized stops, block-tail rule) | 24 `sdny_PRIVATE_ORDER_V2_02032026_partN.csv` (499 MB each, 11.9 GB) plus one `aux_orders.csv` (7.8 GB, 31,261,154 rows) | 31,257,506 in the HALO parts: 31,261,154 events without post-only quoting, of which 143,128 full-position stops were sized from the trader's position and 3,649 with no resolvable or flat position were withheld (they stay in aux); the full day is 1,546,618,738 | **not uploaded to HLRESEARCH**; the first version's part 1 (before `Notional` and sizing) was sent by hand to the test tenant `SDNYTEST` (below) |

**SDNYTEST test upload, 2026-09-29 15:24 UTC (part 1 only, by hand).** HALO
ingests row by row: 1,336,711 of the part's 1,354,000 rows loaded into
`strict_events` (`exchange = 'SDNYTEST'`, `event_type = 'ORDER'`) and 17,289
were rejected, fully explained by two errors:

| Error (Data Audit export) | Rows in part 1 | Cause |
|---|---|---|
| `General error [Error getting market data for symbol USDE]` | 12,371 (every row quoted in USDE, the `hyna` HIP-3 dex; `BTC-HYNA/USDE` and so on) | HALO has no market data for the quote token USDE. USDH and USDT0 rows loaded. Rows without `Price` failed too, so the lookup is per instrument, not per priced row. Executions quoted in USDE load fine on HLRESEARCH (234,378 rows in the April 20 to 26 window) and they carry `Notional`; the orders feed sends none. |
| `Value must be positive number in field [orderQty]` | 5,019 (every `OrderQty = 0` row: full-position TP/SL, §5.8 of the orders mapping) | HALO rejects a zero quantity outright. |

101 rows had both problems (12,371 + 5,019 - 101 = 17,289). The Data Audit
export lists at most 1,000 row identifiers per file (712 + 288 here), so it
understates large problems; a `Row Identifier` N is data row N + 1 of the
CSV (line N + 2 counting the header).

**Test file for the USDE fix (not yet uploaded).** `output/orders_test_usde_20260302/sdny_PRIVATE_ORDER_V2_02032026_part1.csv`:
the `hyna:BTC` market only (`BTC-HYNA/USDE`) for 2026-03-02, post-only
excluded, `Notional` populated on every row (decision 21 of the orders
mapping) and its 1,794 full-position stops sized from the trader's
position (decision 22). 42,371 rows, about 16 MB, no zero quantity left.
Every one of these rows was refused on the first SDNYTEST attempt (USDE
market data, or zero quantity), so the Data Audit result on this file is
a clean yes or no on both fixes. The 24-part March 2 day above was
regenerated with the same code.

Per Solidus's instructions (`solidus_batch_file_upload_instructions.pdf`),
the first orders upload should be a small representative sample checked in
HALO's Data Audit tab before the full set, which is also the cheapest way to
learn whether a zero `OrderQty` is accepted.

Per-day targets for the April 20 to 26 window (rows the exporter wrote):

| Date | Rows | Parts |
|---|---|---|
| 2026-04-20 | 8,660,378 | 11 |
| 2026-04-21 | 8,066,392 | 10 |
| 2026-04-22 | 8,139,178 | 10 |
| 2026-04-23 | 9,442,602 | 12 |
| 2026-04-24 | 8,403,564 | 11 |
| 2026-04-25 | 3,699,044 | 5 |
| 2026-04-26 | 4,552,348 | 6 |

Per-day targets for the May 5 to 16 window (rows the exporter wrote):

| Date | Rows | Parts |
|---|---|---|
| 2026-05-05 | 8,840,822 | 11 |
| 2026-05-06 | 10,938,264 | 14 |
| 2026-05-07 | 10,530,514 | 13 |
| 2026-05-08 | 8,855,510 | 11 |
| 2026-05-09 | 5,761,734 | 7 |
| 2026-05-10 | 6,751,058 | 9 |
| 2026-05-11 | 9,347,014 | 12 |
| 2026-05-12 | 9,511,702 | 12 |
| 2026-05-13 | 9,565,212 | 12 |
| 2026-05-14 | 10,828,142 | 14 |
| 2026-05-15 | 10,348,968 | 13 |
| 2026-05-16 | 5,277,200 | 7 |

Per-day targets for the May 17 to 21 window (rows the exporter wrote):

| Date | Rows | Parts |
|---|---|---|
| 2026-05-17 | 5,589,370 | 7 |
| 2026-05-18 | 11,524,080 | 15 |
| 2026-05-19 | 9,151,896 | 12 |
| 2026-05-20 | 9,796,906 | 12 |
| 2026-05-21 | 11,397,386 | 14 |

Per-day targets are the row counts the exporter reports for each transact
date (the strict files are one row per eligible fill). For the verified
windows above, `strict_events` in ClickHouse matched those counts exactly.

## Housekeeping still open

- **May 19, 20 and 21 exist twice (found 2026-09-24/25, final picture
  2026-09-25 03:30 UTC).** Before this repo's upload the tenant already held,
  with none of it recorded here:

  | Day | Pre-existing rows | Source (`orig_file` prefix, `solidusClient=HLRESEARCH/fileType=linked_private_execution_v2/` omitted) | Id format |
  |---|---|---|---|
  | May 17 | 10 | `date=2026-05-19/hyper manual test - Sheet1.csv` (manual test) | old |
  | May 18 | 48,835 | `date=2026-05-19/solidus_ExecutionData_yesterday_5min.csv` (48,814 rows, 12:00 to 12:05) plus 21 layering test rows from June | old |
  | May 19 | 8,669,402 (+5 test rows) | 31 files `date=2026-05-20/sdny_LINKED_PRIVATE_EXECUTION_V2_19052026_partN` | old (`tradeid-B`/`-S`) |
  | May 20 | 9,117,638 + 679,268 | `date=2026-05-21/sdny_LINKED_PRIVATE_EXECUTION_V2_20052026_partN` (old format) plus a 2026-08-28 backfill of the rows the old gate had missed (current format) | old + current |
  | May 21 | 10,467,798 (10,368,004 distinct) + 1,029,382 | `date=2026-05-21/`, `date=2026-05-22/` and `date=2026-05-23/` files `sdny_LINKED_PRIVATE_EXECUTION_V2_21052026_partN` (old format, 99,794 internal duplicates from overlapping intraday loads) plus a 2026-08-28 backfill (current format) | old + current |

  So the daily pipeline covered May 19 onward from the start, first with the
  old id format and, from the 2026-08-28 backfill on, with the current one.
  This repo's copies use the current format, so they never collide with the
  old-format rows. The loader does de-duplicate on id against rows loaded
  recently (the 2026-08-28 backfill rows blocked the matching 679,268 May 20
  and 1,029,382 May 21 rows from this repo's files, and the nine extra May 5
  copies on 2026-09-23 added nothing), but not against rows loaded months
  ago. Loaded from this repo's files: May 19 = 9,151,896, May 20 = 9,117,638,
  May 21 = 10,368,004. One copy per day has to be purged by Solidus; nothing
  can be undone from the uploader side. `delete_may21_parts.sh` in the output
  folder is only S3 housekeeping now.

### Purge request to send to Solidus (tenant HLRESEARCH, UAT eu-central-1)

Delete from `raw_realtime_matched_executions` (`solidus_client = 'HLRESEARCH'`)
and from `strict_events` (`exchange = 'HLRESEARCH'`, `event_type = 'EXECUTION'`)
every row whose `orig_file` starts with a listed prefix.

Option A, recommended (keep this repo's copies plus the pipeline's 2026-08-28
backfills, so May 19 to 21 carry the current id format like Apr 27 to May 18
and everything after, with no internal duplicates):

1. `date=2026-05-20/sdny_LINKED_PRIVATE_EXECUTION_V2_19052026_` (31 files, 8,669,402 rows)
2. `date=2026-05-21/sdny_LINKED_PRIVATE_EXECUTION_V2_20052026_` (9,117,638 rows)
3. `date=2026-05-21/sdny_LINKED_PRIVATE_EXECUTION_V2_21052026_`, `date=2026-05-22/sdny_LINKED_PRIVATE_EXECUTION_V2_21052026_` and `date=2026-05-23/sdny_LINKED_PRIVATE_EXECUTION_V2_21052026_` (10,467,798 rows together)

Expected result: May 19 = 9,151,896 rows, May 20 = 9,796,906 rows, May 21 =
11,397,386 rows, all ids distinct, plus the few test rows.

Option B (keep the pipeline's originals, purge everything this repo sent for
those three days; May 19 to 21 then stay old-format with May 21's 99,794
internal duplicates):

1. `date=2026-09-24/sdny_LINKED_PRIVATE_EXECUTION_V2_19052026_` (9,151,896 rows)
2. `date=2026-09-24/sdny_LINKED_PRIVATE_EXECUTION_V2_20052026_` (9,117,638 rows)
3. `date=2026-09-24/sdny_LINKED_PRIVATE_EXECUTION_V2_21052026_` (10,368,004 rows)

Optional under either option: the 48,814-row five-minute test file on May 18
(`date=2026-05-19/solidus_ExecutionData_yesterday_5min.csv`) overlaps real
data for 12:00 to 12:05 and could go too.

- **Lesson:** before exporting a window, query `strict_events` for the tenant
  and date range first; "not uploaded" in this ledger only covers uploads
  from this repo. Since 2026-09-25 `hyperliquid-halo check-coverage` runs
  that query per day and per source file, and `export-window` refuses a
  day that already holds rows.

- **May 5 duplicate copies (2026-09-23).** A bug in the first version of the
  upload runner re-sent May 5 parts 1 to 6 after they had already uploaded, so
  the live route folder for upload date 2026-09-23 holds 9 extra objects
  (parts 1 to 3 three times, parts 4 to 6 twice). The runner was fixed and the
  parts recorded as done before anything else was sent. The extra objects are
  listed in `output/halo_strict_20260427_20260516/delete_may5_duplicates.sh`,
  which removes exactly those keys; the owner runs it. Verified 2026-09-24:
  May 5 finished at exactly its target with all ids distinct, so the loader
  did not double-count the copies; deleting them is housekeeping only.

- The 70 parked copies of the January parts sit under
  `s3://solidus-file-watcher-uat-eu-central-1/historicalUploads/solidusClient=HLRESEARCH/`.
  They were never processed. The owner deletes them by hand; never ask Solidus
  to run historical ingestion while they exist, or January doubles.

## How a window gets there

The three `hyperliquid-halo` window subcommands do all of this (README,
"Shipping a window to a tenant"); since 2026-09-25 nothing is scripted by
hand per window. The windows above were shipped with the hand-written
predecessors of these commands, which the output folders no longer hold.

1. Ask the tenant what it already holds, per day and per source file. This
   is the May 19 to 21 lesson: "not uploaded" here only covers this repo.

   ```bash
   hyperliquid-halo check-coverage --start 2026-05-17 --end 2026-05-22 --tenant HLRESEARCH
   ```

2. Export day by day in HALO-strict mode with 499 MB parts (every file stays
   under the 500 MB uploader limit). The command runs the same coverage
   check first and refuses any day that already holds rows
   (`--allow-covered <date>` overrides one day at a time, after reading the
   source files). Each day's outcome goes to `export_window.jsonl` in the
   output folder; re-running skips days already marked ok.

   ```bash
   hyperliquid-halo export-window --start 2026-05-17 --end 2026-05-22 \
     --tenant HLRESEARCH --out-dir output/halo_strict_20260517_20260521
   ```

3. Upload through the live route (file type `LINKED_PRIVATE_EXECUTION_V2`
   for executions, `PRIVATE_ORDER_V2` for orders, chosen by `--feed`;
   never `--historical`). One `halo-upload` call per part, success
   judged by exit code only, every success recorded at once in
   `upload_HLRESEARCH.done`, so a restart never re-sends a part. It can run
   while step 2 is still exporting; it waits for each day's ledger record.

   Solidus's own rules for these files are in
   `solidus_batch_file_upload_instructions.pdf` (Help Center, saved
   2026-09-29): name them `<org>_<fileType>_<DDMMYYYY>_part<N>.csv` with
   nothing but letters, digits and underscores before `.csv`; keep every
   file under 500 MB and put the column header in every split; files of
   similar size between 50 and 300 MB process best (our 499 MB parts have
   worked on every window so far); request a one-time upload link per
   file (valid 15 minutes) and PUT the file to it; `list-files` shows what
   arrived; the Data Audit tab shows processed versus error records per
   file, and ingestion runs hourly. The document also says files older than
   7 days should carry `historical_order` / `historical_execution` in the
   name for the historic route; in practice the historic route only parks
   files (see the January window above) while the live route ingests any
   age, so this repo keeps using the live route. Ingestion is not the same
   as alerting: the TS batch algos only process transact times within the
   last 24 hours, and an algo run over older data is a historic run that
   Solidus schedules on request.

   ```bash
   hyperliquid-halo upload-window --start 2026-05-17 --end 2026-05-22 \
     --tenant HLRESEARCH --out-dir output/halo_strict_20260517_20260521 --yes
   ```

   Files land in
   `s3://solidus-file-watcher-uat-eu-central-1/solidusClient=HLRESEARCH/fileType=linked_private_execution_v2/date=<upload date>/`
   and are ingested automatically whatever the data age.

4. Verify in ClickHouse (`clickhouse-download` skill, `--env uat-eu`,
   `--database solidus_uat_eu`). The raw table fills in about 10 to 25 minutes
   per sweep and `strict_events` a few minutes later; large windows take a few
   hours of hourly loader sweeps.

   ```sql
   SELECT toDate(ts) AS d, count() AS rows, uniqExact(id) AS ids
   FROM strict_events
   WHERE exchange = 'HLRESEARCH' AND event_type = 'EXECUTION'
     AND toDate(ts) BETWEEN '2026-05-05' AND '2026-05-16'
   GROUP BY d ORDER BY d
   ```

   The raw table is `raw_realtime_matched_executions` with tenant column
   `solidus_client` and time column `transact_time`. If the UAT ClickHouse
   endpoint resets connections, ping the other configured hosts; when they
   answer, the outage is that service, so wait instead of re-uploading.

5. Record the window here: dates, part count, rows, upload time, verification.
