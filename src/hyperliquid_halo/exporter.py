"""Runs the HALO mapping query against Snowflake and writes two CSVs.

Two-file output (per user spec):
    * ``halo.csv`` — the strict HALO v2.1 upload file (columns in
      :data:`mapping.HALO_COLUMNS`). Ready for Solidus HALO ingestion.
    * ``aux.csv`` — Hyperliquid-specific supplementary fields, joinable back
      onto ``halo.csv`` on the ``Id`` column. Columns in
      :data:`mapping.AUX_COLUMNS`.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import mapping
from .mapping import AUX_COLUMNS, HALO_COLUMNS, QueryParams
from .snowflake_client import cursor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExportResult:
    """Summary of a single ``export`` run.

    Attributes:
        halo_path: Path to the HALO CSV on disk.
        aux_path: Path to the aux CSV on disk.
        row_count: Total rows written (equal in both files). Each source trade
            emits two rows (buy + sell sides).
    """

    halo_path: Path
    aux_path: Path
    row_count: int


def _row_to_dict(columns: Sequence[str], row: Sequence[Any]) -> dict[str, Any]:
    """Convert a DB row tuple into a dict keyed by the given column names.

    Args:
        columns: Column names from ``cursor.description``.
        row: Tuple returned by ``cursor.fetchone()``/``fetchmany()``.

    Returns:
        Dict mapping column name to value.
    """
    return dict(zip(columns, row, strict=True))


def _halo_default_position_effect(value: Any) -> Any:
    """Apply the HALO export-view default: NULL ``PositionEffect`` -> 'CLOSE'.

    Matches the behaviour of ``11_halo_export_view.sql`` on the reference
    project so that ambiguous position flips and spot trades still produce
    non-null values when written to the HALO CSV. Only applied to the HALO
    file, not the aux file.
    """
    return value if value not in (None, "") else "CLOSE"


def export_to_csv(
    params: QueryParams,
    out_dir: Path,
    *,
    halo_filename: str = "halo.csv",
    aux_filename: str = "aux.csv",
    fetch_size: int = 10_000,
) -> ExportResult:
    """Stream the mapping query to disk as halo.csv + aux.csv.

    Rows are streamed with ``fetchmany`` so that large date ranges do not
    balloon memory; nothing is materialised in a single list.

    Args:
        params: Date range and optional market filters.
        out_dir: Directory to write the CSVs into. Created if missing.
        halo_filename: File name for the strict HALO upload file.
        aux_filename: File name for the supplementary file.
        fetch_size: Snowflake ``fetchmany`` batch size.

    Returns:
        An :class:`ExportResult` describing the written files.

    Raises:
        SnowflakeConfigError: If Snowflake credentials are missing.
        snowflake.connector.errors.ProgrammingError: If the query fails.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    halo_path = out_dir / halo_filename
    aux_path = out_dir / aux_filename

    sql, binds = mapping.build_query(params)
    logger.info("Executing mapping query with binds=%s", binds)

    row_count = 0
    with cursor() as cur:
        cur.execute(sql, binds)
        # Snowflake uppercases unquoted aliases, so the cursor returns e.g.
        # "TRANSACTTIME" while our HALO_COLUMNS use PascalCase. Build an
        # upper() -> cursor-name map so the per-row dict lookups work
        # regardless of how the driver cases column names.
        cursor_columns = [c[0] for c in cur.description]
        cursor_by_upper = {c.upper(): c for c in cursor_columns}
        halo_lookup = {col: cursor_by_upper.get(col.upper(), col) for col in HALO_COLUMNS}
        aux_lookup = {col: cursor_by_upper.get(col.upper(), col) for col in AUX_COLUMNS}

        with halo_path.open("w", newline="", encoding="utf-8") as halo_fh, \
             aux_path.open("w", newline="", encoding="utf-8") as aux_fh:
            halo_writer = csv.DictWriter(halo_fh, fieldnames=HALO_COLUMNS)
            aux_writer = csv.DictWriter(aux_fh, fieldnames=AUX_COLUMNS)
            halo_writer.writeheader()
            aux_writer.writeheader()

            while True:
                batch = cur.fetchmany(fetch_size)
                if not batch:
                    break
                for raw in batch:
                    record = _row_to_dict(cursor_columns, raw)
                    halo_row = {col: record.get(halo_lookup[col]) for col in HALO_COLUMNS}
                    halo_row["PositionEffect"] = _halo_default_position_effect(
                        halo_row["PositionEffect"]
                    )
                    aux_row = {col: record.get(aux_lookup[col]) for col in AUX_COLUMNS}
                    halo_writer.writerow(halo_row)
                    aux_writer.writerow(aux_row)
                    row_count += 1

    logger.info("Wrote %d rows to %s and %s", row_count, halo_path, aux_path)
    return ExportResult(halo_path=halo_path, aux_path=aux_path, row_count=row_count)


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
