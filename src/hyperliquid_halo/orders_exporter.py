"""Runs the HALO orders mapping query against Snowflake and writes two CSVs.

Two-file output (matches the executions exporter pattern):
    * ``halo_orders.csv`` — strict HALO v2.1 Order Data file (columns in
      :data:`orders_mapping.HALO_ORDER_COLUMNS`). Ready for Solidus HALO
      ingestion.
    * ``aux_orders.csv`` — Hyperliquid-specific supplementary fields,
      joinable back onto ``halo_orders.csv`` on ``(Id, TransactTime)``.
      Columns in :data:`orders_mapping.AUX_ORDER_COLUMNS`.

See ``docs/hl_orders_halo_mapping.md`` for the field-by-field rationale.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import orders_mapping
from .orders_mapping import AUX_ORDER_COLUMNS, HALO_ORDER_COLUMNS, OrdersQueryParams
from .snowflake_client import cursor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrdersExportResult:
    """Summary of a single ``export-orders`` run.

    Attributes:
        halo_path: Path to the HALO order CSV on disk.
        aux_path: Path to the aux order CSV on disk.
        row_count: Total rows written (equal in both files). One row per
            source ``RAW.ORDERS`` status-change event after filters.
        unresolved_spot_coins: ``@N`` spot pairs that no ``DEX.TRADES`` row in
            the lookback window could name; their ``Symbol`` is the
            ``@N/USDC`` placeholder. Empty when every spot pair resolved.
        unresolved_spot_rows: Number of rows carrying such a placeholder.
    """

    halo_path: Path
    aux_path: Path
    row_count: int
    unresolved_spot_coins: tuple[str, ...] = ()
    unresolved_spot_rows: int = 0


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
) -> OrdersExportResult:
    """Stream the orders mapping query to disk as halo_orders.csv + aux_orders.csv.

    Rows are streamed with ``fetchmany`` so large date ranges do not balloon
    memory; nothing is materialised in a single list.

    Args:
        params: Date range and optional filters (coin / market_type / user).
        out_dir: Directory to write the CSVs into. Created if missing.
        halo_filename: File name for the strict HALO order upload file.
        aux_filename: File name for the supplementary file.
        fetch_size: Snowflake ``fetchmany`` batch size.

    Returns:
        An :class:`OrdersExportResult` describing the written files.

    Raises:
        SnowflakeConfigError: If Snowflake credentials are missing.
        snowflake.connector.errors.ProgrammingError: If the query fails.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    halo_path = out_dir / halo_filename
    aux_path = out_dir / aux_filename

    sql, binds = orders_mapping.build_orders_query(params)
    logger.info("Executing orders mapping query with binds=%s", binds)

    row_count = 0
    unresolved_rows = 0
    unresolved_coins: set[str] = set()
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

        with halo_path.open("w", newline="", encoding="utf-8") as halo_fh, \
             aux_path.open("w", newline="", encoding="utf-8") as aux_fh:
            halo_writer = csv.DictWriter(halo_fh, fieldnames=HALO_ORDER_COLUMNS)
            aux_writer = csv.DictWriter(aux_fh, fieldnames=AUX_ORDER_COLUMNS)
            halo_writer.writeheader()
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
                    halo_writer.writerow(halo_row)
                    aux_writer.writerow(aux_row)
                    row_count += 1
                    # A Symbol still starting with '@' is an unresolved spot pair
                    # (the lookback join found no trade naming its tokens).
                    symbol = halo_row.get("Symbol")
                    if isinstance(symbol, str) and symbol.startswith("@"):
                        unresolved_rows += 1
                        unresolved_coins.add(str(aux_row.get("_Coin")))

    if unresolved_rows:
        logger.warning(
            "%d order rows on %d spot pair(s) kept the @N/USDC placeholder Symbol "
            "(no DEX.TRADES row in the %d-day lookback named them): %s",
            unresolved_rows, len(unresolved_coins), params.spot_lookback_days,
            ", ".join(sorted(unresolved_coins)),
        )
    logger.info("Wrote %d order rows to %s and %s", row_count, halo_path, aux_path)
    return OrdersExportResult(
        halo_path=halo_path,
        aux_path=aux_path,
        row_count=row_count,
        unresolved_spot_coins=tuple(sorted(unresolved_coins)),
        unresolved_spot_rows=unresolved_rows,
    )


def list_order_coins(params: OrdersQueryParams) -> list[dict[str, Any]]:
    """Return distinct ``COIN`` values with order activity in a date range.

    The orders table has no ``MARKET_TYPE`` / ``PAIR`` columns, so this
    summary uses the ``COIN`` shape (``@N`` or ``base/quote`` → spot;
    everything else → perpetual) to infer the market type.

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
