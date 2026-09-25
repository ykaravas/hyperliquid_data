# HALO upload ledger (tenant HLRESEARCH)

This file records which Hyperliquid execution dates have been shipped from this
repo to the Solidus HALO tenant `HLRESEARCH` (also called `UAT_HYPERLIQUID`,
region `uat-eu-central-1`). Update it every time a window is uploaded or
verified. HALO cannot de-duplicate hand uploads, so a part must never be sent
twice; check this ledger and the ClickHouse counts before re-sending anything.

## Coverage

| Transact dates (UTC) | Source | Parts | Rows in HALO | Uploaded (UTC) | Verified |
|---|---|---|---|---|---|
| 2026-01-12 to 2026-01-16 | this repo, `output/halo_strict_20260112_20260116/` | 71 | 28,839,276 | 2026-09-22 (live route; the first attempt via the historical route only parked copies) | 2026-09-22 17:10, every day at target, ids distinct |
| 2026-04-27 to 2026-05-04 | this repo, `output/halo_strict_20260427_20260516/` | 75 | 58,611,202 | 2026-09-22 23:44 to 2026-09-23 00:30 | 2026-09-23 14:05, every day at target, ids distinct |
| 2026-05-05 to 2026-05-16 | this repo, `output/halo_strict_20260427_20260516/` | 135 (61 GB) | 106,556,140 exported, DQ clean every day | 2026-09-23 19:23 to 22:45 (live route; all 135 parts in S3 with matching sizes) | 2026-09-24 01:16, every day at target, ids distinct; whole Apr 27 to May 16 window = 165,167,342 rows, ids distinct |
| 2026-05-17 to 2026-05-21 | this repo, `output/halo_strict_20260517_20260521/` | 60 (27 GB) | 47,459,638 exported, DQ clean every day | 2026-09-24 21:47 to 23:32 (live route; all 60 parts in S3 once, sizes matching, no retries) | May 17 and 18 verified 2026-09-25 (this repo's rows exactly at target, ids distinct; only a few test rows pre-existed). May 19, 20 and 21 already held a copy each, so those days are double-counted until Solidus purges one copy (loading of this repo's files finished 2026-09-25 01:53); see Housekeeping |
| 2026-05-19 onward | Solidus production pipeline (`defi-hyperliquid-halo`), daily, not from this repo | n/a | n/a | May 19 loaded 2026-05-20, May 20 loaded 2026-05-21, intraday loads from 2026-05-21 04:00 UTC onward; all with the old id format `tradeid-B`/`-S` at first, and a 2026-08-28 backfill in the current format for rows the old gate missed; not May 22 as first assumed | owned by the pipeline |

Not uploaded by anyone as far as this repo knows: 2026-01-01 to 2026-01-11
and 2026-01-17 to 2026-04-26.

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
  from this repo.

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

1. Export strict parts (499 MB cap so every file stays under the 500 MB
   uploader limit):

   ```bash
   hyperliquid-halo export-execs --start 2026-05-05 --end 2026-05-06 \
     --halo-strict --max-file-mb 499 --out-dir output/halo_strict_20260427_20260516
   ```

   For a multi-day window use the single-process driver in the output folder
   (`run_export_driver.py START [END]`), which opens one Snowflake session for
   the whole run and writes one `===== end day` line per date with the row
   count and DQ result.

2. Upload with the `halo-upload` skill through the live route (file type
   `LINKED_PRIVATE_EXECUTION_V2`, no `--historical`). Files land in
   `s3://solidus-file-watcher-uat-eu-central-1/solidusClient=HLRESEARCH/fileType=linked_private_execution_v2/date=<upload date>/`
   and are ingested automatically whatever the data age. Use `--pattern` and
   `--exclude` so that only the new parts are sent when the output folder
   already holds uploaded ones. Dry-run first and confirm the tenant line.

3. Verify in ClickHouse (`clickhouse-download` skill, `--env uat-eu`,
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

4. Record the window here: dates, part count, rows, upload time, verification.
