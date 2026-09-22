"""Runs the HALO mapping query against Snowflake and writes two CSVs.

Two-file output (per user spec):
    * ``halo.csv``: the HALO v2.1 upload file (columns in
      :data:`mapping.HALO_COLUMNS`, or :data:`mapping.HALO_STRICT_COLUMNS`
      when ``QueryParams.halo_strict`` is set, which drops the non-HALO
      ``IsMaker`` column so the file matches production's column set).
    * ``aux.csv`` — Hyperliquid-specific supplementary fields, joinable back
      onto ``halo.csv`` on the ``Id`` column. Columns in
      :data:`mapping.AUX_COLUMNS`.

Every export also runs production's nine data-quality checks
(:mod:`hyperliquid_halo.dq`) over the rows as they stream. In HALO-strict
mode a failing check removes every written file and raises
:class:`dq.DqFailure`, mirroring production's "DQ fails, nothing ships"; in
the default mode the report is logged and returned on :class:`ExportResult`.

Packaging follows production too: in HALO-strict mode (or whenever
``max_part_mb`` is given) the HALO rows are written as one or more part
files per transact date, each capped at ``max_part_mb`` (production's
``R__09`` uses 250 MB, well under HALO's 500 MB limit), named
``{prefix}_LINKED_PRIVATE_EXECUTION_V2_{DDMMYYYY}_part{N}.csv`` exactly as
production's COPY does. The matching aux parts go to an ``aux/``
subdirectory so the output directory itself holds only upload-ready files.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO, Any

from . import mapping
from .dq import DqFailure, DqReport, ExecutionDqChecker
from .mapping import AUX_COLUMNS, HALO_COLUMNS, HALO_STRICT_COLUMNS, QueryParams
from .snowflake_client import cursor

logger = logging.getLogger(__name__)

HALO_FILE_TYPE = "LINKED_PRIVATE_EXECUTION_V2"
"""HALO file type of the executions upload; part of production's file names."""

DEFAULT_PART_MB = 250
"""Production's per-part cap (``R__09`` ``MAX_FILE_SIZE``), well under HALO's 500 MB."""

DEFAULT_FILE_PREFIX = "sdny"
"""Production's file-name prefix (the tenant the pipeline delivers to)."""


@dataclass(frozen=True)
class ExportResult:
    """Summary of a single ``export`` run.

    Attributes:
        halo_path: First (or only) HALO CSV on disk.
        aux_path: First (or only) aux CSV on disk.
        row_count: Total rows written (equal across the HALO and aux files).
            Each source trade emits two rows (buy + sell sides).
        dq_report: Production's DQ-1..DQ-9 evaluated over the written rows.
        halo_paths: Every HALO file written, in order. One entry in
            single-file mode; one per (transact date, part) when packaging
            is on.
        aux_paths: The aux file matching each entry of ``halo_paths``.
    """

    halo_path: Path
    aux_path: Path
    row_count: int
    dq_report: DqReport
    halo_paths: tuple[Path, ...] = ()
    aux_paths: tuple[Path, ...] = ()


class _OutputWriter:
    """Writes matching HALO and aux rows, as one file pair or as dated parts.

    Single-file mode (``max_part_bytes`` is ``None``) writes ``halo_path`` and
    ``aux_path``. Packaged mode writes per-transact-date parts named like
    production's COPY output, rolling to a new part once the current HALO
    part reaches ``max_part_bytes`` (checked every ``size_check_every``
    rows, so a part can overshoot the cap by at most that many rows).
    Rows must arrive ordered by ``TransactTime`` (the query guarantees it).

    Args:
        out_dir: Output directory (created by the caller).
        halo_columns: HALO CSV column order.
        aux_columns: Aux CSV column order.
        halo_path: Single-file mode HALO path.
        aux_path: Single-file mode aux path.
        max_part_bytes: Per-part size cap in bytes, or ``None`` for one file.
        file_prefix: Production-style file-name prefix (``sdny``).
        size_check_every: How many rows between size checks.
    """

    def __init__(
        self,
        out_dir: Path,
        halo_columns: Sequence[str],
        aux_columns: Sequence[str],
        *,
        halo_path: Path,
        aux_path: Path,
        max_part_bytes: int | None,
        file_prefix: str,
        size_check_every: int = 1000,
    ) -> None:
        self._out_dir = out_dir
        self._halo_columns = list(halo_columns)
        self._aux_columns = list(aux_columns)
        self._single_halo = halo_path
        self._single_aux = aux_path
        self._max = max_part_bytes
        self._prefix = file_prefix
        self._check_every = max(1, size_check_every)
        self._day: date | None = None
        self._part = 0
        self._rows_in_part = 0
        self._halo_fh: IO[str] | None = None
        self._aux_fh: IO[str] | None = None
        self._halo_writer: csv.DictWriter[str] | None = None
        self._aux_writer: csv.DictWriter[str] | None = None
        self.halo_paths: list[Path] = []
        self.aux_paths: list[Path] = []

    @property
    def packaged(self) -> bool:
        """``True`` when writing dated, size-capped parts."""
        return self._max is not None

    def _open(self, halo_path: Path, aux_path: Path) -> None:
        self.close_current()
        halo_path.parent.mkdir(parents=True, exist_ok=True)
        aux_path.parent.mkdir(parents=True, exist_ok=True)
        self._halo_fh = halo_path.open("w", newline="", encoding="utf-8")
        self._aux_fh = aux_path.open("w", newline="", encoding="utf-8")
        self._halo_writer = csv.DictWriter(self._halo_fh, fieldnames=self._halo_columns)
        self._aux_writer = csv.DictWriter(self._aux_fh, fieldnames=self._aux_columns)
        self._halo_writer.writeheader()
        self._aux_writer.writeheader()
        self._rows_in_part = 0
        self.halo_paths.append(halo_path)
        self.aux_paths.append(aux_path)

    def _open_part(self, day: date, part: int) -> None:
        self._day, self._part = day, part
        name = f"{self._prefix}_{HALO_FILE_TYPE}_{day.strftime('%d%m%Y')}_part{part}.csv"
        self._open(self._out_dir / name, self._out_dir / "aux" / name)

    def _current_halo_bytes(self) -> int:
        assert self._halo_fh is not None
        self._halo_fh.flush()
        return self._halo_fh.buffer.tell()  # type: ignore[attr-defined]

    def write(self, halo_row: dict[str, Any], aux_row: dict[str, Any]) -> None:
        """Write one row pair, opening or rolling files as needed.

        Args:
            halo_row: HALO-column values; ``TransactTime`` (epoch ms) decides
                the part's date in packaged mode.
            aux_row: Aux-column values.
        """
        if not self.packaged:
            if self._halo_writer is None:
                self._open(self._single_halo, self._single_aux)
        else:
            day = datetime.fromtimestamp(int(halo_row["TransactTime"]) / 1000, tz=UTC).date()
            if self._halo_writer is None or day != self._day:
                self._open_part(day, 1)
            elif (
                self._rows_in_part % self._check_every == 0
                and self._current_halo_bytes() >= (self._max or 0)
            ):
                self._open_part(day, self._part + 1)
        assert self._halo_writer is not None and self._aux_writer is not None
        self._halo_writer.writerow(halo_row)
        self._aux_writer.writerow(aux_row)
        self._rows_in_part += 1

    def close_current(self) -> None:
        """Close the files currently open, if any."""
        for fh in (self._halo_fh, self._aux_fh):
            if fh is not None:
                fh.close()
        self._halo_fh = self._aux_fh = None
        self._halo_writer = self._aux_writer = None

    def finish(self) -> None:
        """Close everything; in single-file mode make sure the files exist."""
        if not self.packaged and self._halo_writer is None:
            self._open(self._single_halo, self._single_aux)
        self.close_current()

    def remove_all(self) -> None:
        """Delete every file written so far (DQ failure in strict mode)."""
        self.close_current()
        for path in self.halo_paths + self.aux_paths:
            path.unlink(missing_ok=True)


def _row_to_dict(columns: Sequence[str], row: Sequence[Any]) -> dict[str, Any]:
    """Convert a DB row tuple into a dict keyed by the given column names.

    Args:
        columns: Column names from ``cursor.description``.
        row: Tuple returned by ``cursor.fetchone()``/``fetchmany()``.

    Returns:
        Dict mapping column name to value.
    """
    return dict(zip(columns, row, strict=True))


def export_to_csv(
    params: QueryParams,
    out_dir: Path,
    *,
    halo_filename: str = "halo.csv",
    aux_filename: str = "aux.csv",
    fetch_size: int = 10_000,
    max_part_mb: float | None = None,
    file_prefix: str = DEFAULT_FILE_PREFIX,
) -> ExportResult:
    """Stream the mapping query to disk as HALO + aux CSV files.

    Rows are streamed with ``fetchmany`` so that large date ranges do not
    balloon memory; nothing is materialised in a single list.

    Args:
        params: Date range, optional market filters, and the eligibility /
            HALO-strict switches. ``params.halo_strict`` selects
            :data:`HALO_STRICT_COLUMNS` and turns packaging on.
        out_dir: Directory to write the CSVs into. Created if missing.
        halo_filename: Single-file mode name for the HALO file.
        aux_filename: Single-file mode name for the aux file.
        fetch_size: Snowflake ``fetchmany`` batch size.
        max_part_mb: Per-part cap in MB. When set, output is packaged as
            per-transact-date parts named like production's
            (``{prefix}_LINKED_PRIVATE_EXECUTION_V2_DDMMYYYY_partN.csv``) with
            aux parts under ``aux/``. ``None`` keeps single files, except in
            HALO-strict mode where it defaults to :data:`DEFAULT_PART_MB`.
        file_prefix: Prefix for packaged file names (production: ``sdny``).

    Returns:
        An :class:`ExportResult` describing the written files.

    Raises:
        SnowflakeConfigError: If Snowflake credentials are missing.
        snowflake.connector.errors.ProgrammingError: If the query fails.
        DqFailure: In HALO-strict mode, when a blocking DQ check fails. Every
            file written so far is removed first.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if max_part_mb is None and params.halo_strict:
        max_part_mb = DEFAULT_PART_MB
    max_part_bytes = int(max_part_mb * 1_000_000) if max_part_mb else None
    writer = _OutputWriter(
        out_dir,
        HALO_STRICT_COLUMNS if params.halo_strict else HALO_COLUMNS,
        AUX_COLUMNS,
        halo_path=out_dir / halo_filename,
        aux_path=out_dir / aux_filename,
        max_part_bytes=max_part_bytes,
        file_prefix=file_prefix,
    )

    sql, binds = mapping.build_query(params)
    checker = ExecutionDqChecker()
    halo_columns = writer._halo_columns
    logger.info(
        "Executing mapping query with binds=%s (halo_strict=%s, include_ineligible=%s, "
        "packaging=%s)",
        binds, params.halo_strict, params.include_ineligible,
        f"{max_part_mb} MB parts" if max_part_bytes else "single file",
    )

    row_count = 0
    try:
        with cursor() as cur:
            cur.execute(sql, binds)
            # Snowflake uppercases unquoted aliases, so the cursor returns e.g.
            # "TRANSACTTIME" while our HALO_COLUMNS use PascalCase. Build an
            # upper() -> cursor-name map so the per-row dict lookups work
            # regardless of how the driver cases column names.
            cursor_columns = [c[0] for c in cur.description]
            cursor_by_upper = {c.upper(): c for c in cursor_columns}
            halo_lookup = {col: cursor_by_upper.get(col.upper(), col) for col in halo_columns}
            aux_lookup = {col: cursor_by_upper.get(col.upper(), col) for col in AUX_COLUMNS}

            while True:
                batch = cur.fetchmany(fetch_size)
                if not batch:
                    break
                for raw in batch:
                    record = _row_to_dict(cursor_columns, raw)
                    # NULL PositionEffect (spot rows, flips, other directions with no
                    # clean open/close semantics) is written as empty on purpose --
                    # HALO stores it as empty and applies no default, and forcing
                    # 'CLOSE' (removed 2026-08-24) mislabeled those rows.
                    halo_row = {col: record.get(halo_lookup[col]) for col in halo_columns}
                    aux_row = {col: record.get(aux_lookup[col]) for col in AUX_COLUMNS}
                    checker.observe(halo_row, aux_row)
                    writer.write(halo_row, aux_row)
                    row_count += 1
        writer.finish()
    except BaseException:
        writer.close_current()
        raise

    report = checker.finish()
    if params.halo_strict and not report.ok:
        # Production aborts before COPY when DQ fails; the equivalent here is
        # to leave nothing behind that could be uploaded by mistake.
        removed = list(writer.halo_paths)
        writer.remove_all()
        logger.error("HALO-strict export failed DQ; removed %d file pair(s)\n%s",
                     len(removed), report.describe())
        raise DqFailure(report)
    for result in report.failures:
        logger.warning("DQ %s failed (would block --halo-strict): %s; observed %s",
                       result.check, result.description, result.observed)
    for result in report.warnings:
        logger.warning("DQ %s: %s", result.check, result.summary)

    halo_paths, aux_paths = tuple(writer.halo_paths), tuple(writer.aux_paths)
    logger.info("Wrote %d rows to %d HALO file(s) under %s", row_count, len(halo_paths), out_dir)
    return ExportResult(
        halo_path=halo_paths[0],
        aux_path=aux_paths[0],
        row_count=row_count,
        dq_report=report,
        halo_paths=halo_paths,
        aux_paths=aux_paths,
    )


def list_markets(params: QueryParams) -> list[dict[str, Any]]:
    """Return the distinct markets with trade activity in a date range.

    Useful for picking a ``--coin`` / ``--market-type`` filter before running
    a full export. The date filters on ``params`` are the only inputs used —
    any market filters set on ``params`` are ignored so the summary always
    reflects the full date range.

    Args:
        params: Query parameters (only ``start_ts`` and ``end_ts`` are read).

    Returns:
        A list of dicts, one per distinct (market_type, coin, pair, tokenA,
        tokenB) combination, sorted by trade count descending.
    """
    binds: dict[str, Any] = {
        "start_ts": params.start_ts,
        "end_ts": params.end_ts,
    }
    with cursor() as cur:
        cur.execute(mapping.LIST_MARKETS_SQL, binds)
        columns = [c[0] for c in cur.description]
        return [_row_to_dict(columns, row) for row in cur.fetchall()]
