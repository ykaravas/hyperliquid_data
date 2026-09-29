"""Runs the HALO orders mapping query against Snowflake and writes two CSVs.

Two-file output (matches the executions exporter pattern):
    * ``halo_orders.csv`` — strict HALO v2.1 Order Data file (columns in
      :data:`orders_mapping.HALO_ORDER_COLUMNS`). Ready for Solidus HALO
      ingestion.
    * ``aux_orders.csv`` — Hyperliquid-specific supplementary fields,
      joinable back onto ``halo_orders.csv`` on ``(Id, TransactTime)``.
      Columns in :data:`orders_mapping.AUX_ORDER_COLUMNS`.

Full-position TP/SL orders arrive from Allium with ``ORIGINAL_SIZE = 0``
(Hyperliquid sizes them to the position when they trigger). The SQL sizes
them from the trader's position at placement (aux ``_PositionSize`` shows
when that happened); the few that cannot be sized keep ``OrderQty = 0``,
which HALO rejects, so ``drop_zero_qty`` (the default) withholds them from
the HALO file while keeping them in aux and counting them, never silently.
See ``docs/hl_orders_halo_mapping.md`` §5.8 and the field-by-field rationale.

``halo_strict`` packages the HALO rows the way HALO is uploaded: size-capped
part files named ``{prefix}_PRIVATE_ORDER_V2_{DDMMYYYY}_part{N}.csv`` (the
date is the export day, since rows are selected by ``ORDER_TIMESTAMP`` day
and their ``TransactTime`` can run months later), one day per run. The aux
file stays a single ``aux_orders.csv`` in either mode.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import IO, Any

from . import orders_mapping
from .exporter import DEFAULT_FILE_PREFIX
from .orders_mapping import AUX_ORDER_COLUMNS, HALO_ORDER_COLUMNS, OrdersQueryParams
from .snowflake_client import cursor

logger = logging.getLogger(__name__)

ORDERS_HALO_FILE_TYPE = "PRIVATE_ORDER_V2"
"""HALO file type of the orders upload (v2 and v2.1 order files); part of the strict names."""

DEFAULT_ORDERS_PART_MB = 499
"""Per-part cap for strict packaging, just under HALO's 500 MB file limit (as export-window)."""


@dataclass(frozen=True)
class OrdersExportResult:
    """Summary of a single ``export-orders`` run.

    Attributes:
        halo_path: Path to the HALO order CSV on disk.
        aux_path: Path to the aux order CSV on disk.
        row_count: Rows written to the HALO file. One row per source
            ``RAW.ORDERS`` status-change event after filters; the aux file
            holds the same rows plus any withheld zero-quantity rows.
        unresolved_spot_coins: ``@N`` spot pairs that no ``DEX.TRADES`` row in
            the lookback window could name; their ``Symbol`` is the
            ``@N/USDC`` placeholder. Empty when every spot pair resolved.
        unresolved_spot_rows: Number of rows carrying such a placeholder.
        zero_qty_rows: Rows whose ``OrderQty`` is still 0 after the
            position lookup (full-position TP/SL orders whose trader had no
            resolvable position). Counted whether or not they were written
            to the HALO file.
        zero_qty_dropped: ``True`` when those rows were withheld from the
            HALO file (``drop_zero_qty``); they are always kept in aux.
        sized_from_position_rows: Rows whose ``OrderQty`` came from the
            trader's position at placement (aux ``_PositionSize`` set).
        halo_paths: Every HALO file written: the single file, or the strict
            parts in order. ``halo_path`` is the first of them.
    """

    halo_path: Path
    aux_path: Path
    row_count: int
    unresolved_spot_coins: tuple[str, ...] = ()
    unresolved_spot_rows: int = 0
    zero_qty_rows: int = 0
    zero_qty_dropped: bool = False
    halo_paths: tuple[Path, ...] = ()
    sized_from_position_rows: int = 0


class _HaloOrdersWriter:
    """Writes HALO order rows as one file or as size-capped strict parts.

    In strict mode a new part is opened once the current one reaches
    ``max_bytes`` (checked every ``size_check_every`` rows, so a part can
    overshoot by at most that many rows). Parts are named
    ``{prefix}_PRIVATE_ORDER_V2_{DDMMYYYY}_part{N}.csv`` with the export day,
    not the row's ``TransactTime`` (an order's later lifecycle rows can sit
    months after the day it was placed).

    Args:
        single_path: Path of the one HALO file in non-strict mode.
        out_dir: Directory for strict parts.
        day: Export day used in the part names.
        max_bytes: Per-part cap in strict mode, or ``None`` for one file.
        file_prefix: Production-style prefix (``sdny``).
        size_check_every: Rows between size checks in strict mode.
    """

    def __init__(
        self,
        single_path: Path,
        *,
        out_dir: Path,
        day: date,
        max_bytes: int | None,
        file_prefix: str,
        size_check_every: int = 1000,
    ) -> None:
        self._single_path = single_path
        self._out_dir = out_dir
        self._day = day
        self._max = max_bytes
        self._prefix = file_prefix
        self._check_every = max(1, size_check_every)
        self._part = 0
        self._rows_in_part = 0
        self._fh: IO[str] | None = None
        self._writer: csv.DictWriter[str] | None = None
        self.paths: list[Path] = []

    @property
    def packaged(self) -> bool:
        """``True`` when writing strict parts."""
        return self._max is not None

    def _open(self, path: Path) -> None:
        self.close()
        self._fh = path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=HALO_ORDER_COLUMNS)
        self._writer.writeheader()
        self._rows_in_part = 0
        self.paths.append(path)

    def _open_part(self, part: int) -> None:
        self._part = part
        name = f"{self._prefix}_{ORDERS_HALO_FILE_TYPE}_{self._day:%d%m%Y}_part{part}.csv"
        self._open(self._out_dir / name)

    def _current_bytes(self) -> int:
        assert self._fh is not None
        self._fh.flush()
        return self._fh.buffer.tell()  # type: ignore[attr-defined]

    def write(self, row: dict[str, Any]) -> None:
        """Write one HALO row, opening the file or rolling to a new part as needed."""
        if self._writer is None:
            self._open_part(1) if self.packaged else self._open(self._single_path)
        elif (
            self.packaged
            and self._rows_in_part % self._check_every == 0
            and self._current_bytes() >= (self._max or 0)
        ):
            self._open_part(self._part + 1)
        assert self._writer is not None
        self._writer.writerow(row)
        self._rows_in_part += 1

    def close(self) -> None:
        """Close the file currently open, if any."""
        if self._fh is not None:
            self._fh.close()
        self._fh = self._writer = None

    def finish(self) -> None:
        """Close everything; make sure at least one file exists."""
        if self._writer is None:
            self._open_part(1) if self.packaged else self._open(self._single_path)
        self.close()


def _is_zero_quantity(value: Any) -> bool:
    """Return ``True`` when a CSV quantity value is numerically zero.

    Args:
        value: The ``OrderQty`` value as it came off the cursor (a string such
            as ``'0.0'``, a number, or ``None``).

    Returns:
        ``True`` for a parseable value equal to zero; ``False`` for anything
        else, including ``None`` and non-numeric text.
    """
    if value is None:
        return False
    try:
        return Decimal(str(value)) == 0
    except InvalidOperation:
        return False


def _row_to_dict(columns: Sequence[str], row: Sequence[Any]) -> dict[str, Any]:
    """Convert a DB row tuple into a dict keyed by the given column names.

    Args:
        columns: Column names from ``cursor.description``.
        row: Tuple returned by ``cursor.fetchone()``/``fetchmany()``.

    Returns:
        Dict mapping column name to value.
    """
    return dict(zip(columns, row, strict=True))


def export_orders_to_csv(
    params: OrdersQueryParams,
    out_dir: Path,
    *,
    halo_filename: str = "halo_orders.csv",
    aux_filename: str = "aux_orders.csv",
    fetch_size: int = 10_000,
    drop_zero_qty: bool = True,
    halo_strict: bool = False,
    max_part_mb: float = DEFAULT_ORDERS_PART_MB,
    file_prefix: str = DEFAULT_FILE_PREFIX,
    size_check_every: int = 1000,
) -> OrdersExportResult:
    """Stream the orders mapping query to disk as HALO order file(s) + aux_orders.csv.

    Rows are streamed with ``fetchmany`` so large date ranges do not balloon
    memory; nothing is materialised in a single list.

    Args:
        params: Date range and optional filters (coin / market_type / user).
        out_dir: Directory to write the CSVs into. Created if missing.
        halo_filename: File name for the HALO order file (non-strict mode).
        aux_filename: File name for the supplementary file (both modes).
        fetch_size: Snowflake ``fetchmany`` batch size.
        drop_zero_qty: Withhold rows whose ``OrderQty`` is still 0 after
            the position lookup from the HALO file (HALO rejects a zero
            quantity). They stay in aux and are counted in
            :attr:`OrdersExportResult.zero_qty_rows` either way. Default
            ``True``; ``False`` ships them as they are (§5.8).
        halo_strict: Package the HALO rows as size-capped parts named
            ``{file_prefix}_PRIVATE_ORDER_V2_{DDMMYYYY}_part{N}.csv``, ready
            for upload. Requires a one-day window so the name carries one
            date. The aux file is still written once, as ``aux_filename``.
        max_part_mb: Per-part cap in MB in strict mode (default 499).
        file_prefix: Prefix of the strict part names (production: ``sdny``).
        size_check_every: Rows between part-size checks in strict mode.

    Returns:
        An :class:`OrdersExportResult` describing the written files.

    Raises:
        ValueError: If ``halo_strict`` is set on a window other than one day.
        SnowflakeConfigError: If Snowflake credentials are missing.
        snowflake.connector.errors.ProgrammingError: If the query fails.
    """
    if halo_strict and params.end_ts - params.start_ts != timedelta(days=1):
        raise ValueError(
            "halo_strict exports one day per run so the part names carry a single date; "
            f"got {params.start_ts.date()} .. {params.end_ts.date()}"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    halo_path = out_dir / halo_filename
    aux_path = out_dir / aux_filename
    halo_writer = _HaloOrdersWriter(
        halo_path,
        out_dir=out_dir,
        day=params.start_ts.date(),
        max_bytes=int(max_part_mb * 1_000_000) if halo_strict else None,
        file_prefix=file_prefix,
        size_check_every=size_check_every,
    )

    sql, binds = orders_mapping.build_orders_query(params)
    logger.info("Executing orders mapping query with binds=%s", binds)

    row_count = 0
    unresolved_rows = 0
    unresolved_coins: set[str] = set()
    zero_qty_rows = 0
    sized_rows = 0
    with cursor() as cur:
        cur.execute(sql, binds)
        # Snowflake uppercases unquoted aliases; build an upper() -> cursor-name
        # map so the per-row dict lookups work regardless of how the driver
        # cases column names (same pattern as the executions exporter).
        cursor_columns = [c[0] for c in cur.description]
        cursor_by_upper = {c.upper(): c for c in cursor_columns}
        halo_lookup = {
            col: cursor_by_upper.get(col.upper(), col) for col in HALO_ORDER_COLUMNS
        }
        aux_lookup = {
            col: cursor_by_upper.get(col.upper(), col) for col in AUX_ORDER_COLUMNS
        }

        with aux_path.open("w", newline="", encoding="utf-8") as aux_fh:
            aux_writer = csv.DictWriter(aux_fh, fieldnames=AUX_ORDER_COLUMNS)
            aux_writer.writeheader()

            while True:
                batch = cur.fetchmany(fetch_size)
                if not batch:
                    break
                for raw in batch:
                    record = _row_to_dict(cursor_columns, raw)
                    halo_row = {
                        col: record.get(halo_lookup[col]) for col in HALO_ORDER_COLUMNS
                    }
                    aux_row = {
                        col: record.get(aux_lookup[col]) for col in AUX_ORDER_COLUMNS
                    }
                    # Full-position TP/SL orders (§5.8): sized from the position
                    # when possible; a row still at OrderQty 0 is always counted,
                    # always in aux, and withheld from HALO by default.
                    aux_writer.writerow(aux_row)
                    if aux_row.get("_PositionSize") not in (None, ""):
                        sized_rows += 1
                    if _is_zero_quantity(halo_row.get("OrderQty")):
                        zero_qty_rows += 1
                        if drop_zero_qty:
                            continue
                    halo_writer.write(halo_row)
                    row_count += 1
                    # A Symbol still starting with '@' is an unresolved spot pair
                    # (the lookback join found no trade naming its tokens).
                    symbol = halo_row.get("Symbol")
                    if isinstance(symbol, str) and symbol.startswith("@"):
                        unresolved_rows += 1
                        unresolved_coins.add(str(aux_row.get("_Coin")))
            halo_writer.finish()

    if unresolved_rows:
        logger.warning(
            "%d order rows on %d spot pair(s) kept the @N/USDC placeholder Symbol "
            "(no DEX.TRADES row in the %d-day lookback named them): %s",
            unresolved_rows, len(unresolved_coins), params.spot_lookback_days,
            ", ".join(sorted(unresolved_coins)),
        )
    if sized_rows:
        logger.info(
            "%d full-position TP/SL rows were sized from the trader's position at placement "
            "(aux _PositionSize)", sized_rows,
        )
    if zero_qty_rows:
        logger.warning(
            "%d order rows still have OrderQty 0 (full-position TP/SL orders with no "
            "resolvable position): %s",
            zero_qty_rows,
            "withheld from the HALO file, kept in aux" if drop_zero_qty
            else "kept in the HALO file with OrderQty 0, which HALO rejects (§5.8)",
        )
    halo_paths = tuple(halo_writer.paths)
    logger.info(
        "Wrote %d order rows to %d HALO file(s) starting %s and %s",
        row_count, len(halo_paths), halo_paths[0], aux_path,
    )
    return OrdersExportResult(
        halo_path=halo_paths[0],
        aux_path=aux_path,
        row_count=row_count,
        unresolved_spot_coins=tuple(sorted(unresolved_coins)),
        unresolved_spot_rows=unresolved_rows,
        zero_qty_rows=zero_qty_rows,
        zero_qty_dropped=drop_zero_qty,
        halo_paths=halo_paths,
        sized_from_position_rows=sized_rows,
    )


def list_order_coins(params: OrdersQueryParams) -> list[dict[str, Any]]:
    """Return distinct ``COIN`` values with order activity in a date range.

    The orders table has no ``MARKET_TYPE`` / ``PAIR`` columns, so this
    summary uses the ``COIN`` shape (``@N`` or ``base/quote`` → spot;
    ``#N`` → ``outcome (excluded)``, the HIP-4 markets ``export-orders``
    drops at source; everything else → perpetual) to infer the market type.

    Args:
        params: Query parameters (only ``start_ts`` and ``end_ts`` are
            read — any other filters set on ``params`` are ignored so the
            summary always reflects the full date range).

    Returns:
        A list of dicts, one per distinct ``(COIN, inferred_market_type)``
        combination, sorted by event count descending. Each dict has keys
        ``COIN``, ``INFERRED_MARKET_TYPE``, ``EVENT_COUNT``,
        ``UNIQUE_ORDERS``, ``UNIQUE_USERS``.
    """
    binds: dict[str, Any] = {
        "start_ts": params.start_ts,
        "end_ts": params.end_ts,
    }
    with cursor() as cur:
        cur.execute(orders_mapping.LIST_ORDER_COINS_SQL, binds)
        columns = [c[0] for c in cur.description]
        return [_row_to_dict(columns, row) for row in cur.fetchall()]
