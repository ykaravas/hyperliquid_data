"""Per-day HALO-strict export of a date window, with a resumable status ledger.

``export-execs`` handles one date range in one query. Shipping a multi-day
window to a tenant needs a little more around it, and this module is where
that lives so it is never re-written by hand for each window:

* **One query per day.** Each day is its own Snowflake query, its own DQ
  report and its own set of part files, so a failure on one day never
  discards the others, and the uploader can start on finished days while
  later days are still exporting.
* **A machine-readable ledger.** Every finished day appends one JSON line to
  ``export_window.jsonl`` in the output directory (see :class:`DayRecord`):
  status, row count and the exact part file names. The uploader
  (:mod:`hyperliquid_halo.upload`) reads this file instead of parsing a
  free-form log, and only ever sends parts an ``ok`` record names.
* **Deterministic restarts.** Re-running the same window skips days whose
  latest record is ``ok`` (``force=True`` re-exports them) and deletes any
  stale part files of a day before exporting it again, so a folder never
  mixes parts from two attempts.
* **The tenant coverage gate** lives in :mod:`hyperliquid_halo.coverage`;
  the CLI runs it before calling :func:`export_window` so a day that is
  already on the tenant is never exported by accident.
* **Two feeds, one procedure.** A :class:`~hyperliquid_halo.feeds.Feed`
  selects the exporter and the HALO file type: executions
  (``LINKED_PRIVATE_EXECUTION_V2`` parts with aux parts under ``aux/``) or
  orders (``PRIVATE_ORDER_V2`` parts with one ``aux_orders_DDMMYYYY.csv``
  per day). Both feeds can be limited to a list of markets.

Parts are capped at :data:`DEFAULT_WINDOW_PART_MB` (499 MB) rather than the
exporter's production default of 250 MB: fewer, larger files upload faster
and still sit under HALO's 500 MB per-file limit.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

from .dq import DqFailure
from .exporter import DEFAULT_FILE_PREFIX, export_to_csv
from .feeds import EXECUTIONS, ORDERS, Feed
from .mapping import QueryParams
from .orders_exporter import export_orders_to_csv
from .orders_mapping import OrdersQueryParams

logger = logging.getLogger(__name__)

STATUS_FILENAME = "export_window.jsonl"
"""Per-window ledger written next to the parts; one JSON object per finished day."""

DEFAULT_WINDOW_PART_MB = 499
"""Per-part cap for window exports: the largest size that stays under HALO's 500 MB limit."""

STATUS_OK = "ok"
STATUS_DQ_FAILED = "dq_failed"
STATUS_ERROR = "error"

_PART_NUMBER_RE = re.compile(r"_part(\d+)\.csv$", re.IGNORECASE)


@dataclass(frozen=True)
class DayRecord:
    """One line of the window ledger: the outcome of exporting a single day.

    Attributes:
        day: Transact date (UTC) the record is about.
        status: :data:`STATUS_OK`, :data:`STATUS_DQ_FAILED` or
            :data:`STATUS_ERROR`.
        rows: HALO rows written (``0`` unless ``ok``).
        parts: HALO part file names written for the day, in part order.
            Empty unless ``ok``; the uploader sends exactly these names.
        finished_at: ISO-8601 UTC timestamp of when the day finished.
        seconds: Wall-clock duration of the day's export.
        error: Failure detail (the DQ report or the exception text); empty
            when ``ok``.
    """

    day: date
    status: str
    rows: int = 0
    parts: tuple[str, ...] = ()
    finished_at: str = ""
    seconds: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        """``True`` when the day exported cleanly."""
        return self.status == STATUS_OK

    def to_json(self) -> str:
        """Serialize as one JSON line (no trailing newline)."""
        payload = asdict(self)
        payload["day"] = self.day.isoformat()
        payload["parts"] = list(self.parts)
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> DayRecord:
        """Parse a line written by :meth:`to_json`.

        Args:
            line: One JSON object as text.

        Returns:
            The decoded record.

        Raises:
            ValueError: If the line is not valid JSON or lacks required keys.
        """
        try:
            payload = json.loads(line)
            return cls(
                day=date.fromisoformat(payload["day"]),
                status=str(payload["status"]),
                rows=int(payload.get("rows", 0)),
                parts=tuple(payload.get("parts", ())),
                finished_at=str(payload.get("finished_at", "")),
                seconds=float(payload.get("seconds", 0.0)),
                error=str(payload.get("error", "")),
            )
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Malformed ledger line: {line[:120]!r}") from exc


@dataclass(frozen=True)
class WindowResult:
    """Outcome of :func:`export_window` over every requested day.

    Attributes:
        records: The record produced for each day exported in this run, in
            date order (days skipped as already ``ok`` are not included).
        skipped: Days skipped because an earlier run already exported them.
    """

    records: tuple[DayRecord, ...] = ()
    skipped: tuple[date, ...] = field(default_factory=tuple)

    @property
    def failed(self) -> tuple[DayRecord, ...]:
        """Records of days that did not export cleanly."""
        return tuple(r for r in self.records if not r.ok)

    @property
    def ok(self) -> bool:
        """``True`` when every day exported in this run succeeded."""
        return not self.failed


def status_path(out_dir: Path) -> Path:
    """Path of the window ledger inside ``out_dir``."""
    return out_dir / STATUS_FILENAME


def read_status(out_dir: Path) -> dict[date, DayRecord]:
    """Load the window ledger; the latest record for a day wins.

    Args:
        out_dir: Output directory holding ``export_window.jsonl``.

    Returns:
        Mapping of day to its most recent record. Empty when no ledger exists.

    Raises:
        ValueError: If a ledger line cannot be parsed (the file was edited by
            hand); nothing is guessed in that case.
    """
    path = status_path(out_dir)
    if not path.exists():
        return {}
    latest: dict[date, DayRecord] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = DayRecord.from_json(line)
            latest[record.day] = record
    return latest


def append_status(out_dir: Path, record: DayRecord) -> None:
    """Append one record to the window ledger, creating the file if needed."""
    with status_path(out_dir).open("a", encoding="utf-8") as fh:
        fh.write(record.to_json() + "\n")
        fh.flush()


def part_number(name: str) -> int:
    """Extract ``N`` from a ``..._partN.csv`` part name.

    Args:
        name: A part file name.

    Returns:
        The integer part number.

    Raises:
        ValueError: If the name has no ``_partN.csv`` suffix.
    """
    match = _PART_NUMBER_RE.search(name)
    if match is None:
        raise ValueError(f"Not a part file name: {name!r}")
    return int(match.group(1))


def part_glob(day: date, file_prefix: str = DEFAULT_FILE_PREFIX, feed: Feed = EXECUTIONS) -> str:
    """Glob matching every HALO part of ``day`` (production's ``DDMMYYYY`` naming)."""
    return f"{file_prefix}_{feed.file_type}_{day:%d%m%Y}_part*.csv"


def orders_aux_filename(day: date) -> str:
    """Name of the single aux file an orders day writes next to its parts."""
    return f"aux_orders_{day:%d%m%Y}.csv"


def day_parts(
    out_dir: Path, day: date, file_prefix: str = DEFAULT_FILE_PREFIX, feed: Feed = EXECUTIONS
) -> list[Path]:
    """HALO part files of ``day`` present on disk, sorted by part number.

    Sorting is numeric so ``part10`` follows ``part9`` rather than ``part1``.
    """
    return sorted(out_dir.glob(part_glob(day, file_prefix, feed)),
                  key=lambda p: part_number(p.name))


def remove_stale_parts(
    out_dir: Path, day: date, file_prefix: str = DEFAULT_FILE_PREFIX, feed: Feed = EXECUTIONS
) -> int:
    """Delete every HALO and aux file of ``day`` left behind by an earlier attempt.

    Called before a day is (re-)exported so the folder never holds parts
    from two attempts (an interrupted export can leave a partial trailing
    part; a re-export that rolls parts differently would otherwise leave an
    orphan with a higher part number). For executions the aux parts under
    ``aux/`` go too; for orders the day's single aux file does.

    Args:
        out_dir: Output directory.
        day: Transact date whose parts to remove.
        file_prefix: Part-name prefix used by the export.
        feed: Which feed's files to look for.

    Returns:
        Number of files removed (HALO and aux together).
    """
    stale = list(day_parts(out_dir, day, file_prefix, feed))
    if feed is ORDERS:
        aux = out_dir / orders_aux_filename(day)
        if aux.exists():
            stale.append(aux)
    else:
        stale.extend((out_dir / "aux").glob(part_glob(day, file_prefix, feed)))
    for path in stale:
        path.unlink()
    if stale:
        logger.warning("Removed %d stale file(s) for %s before re-export", len(stale), day)
    return len(stale)


def iter_days(start: date, end_exclusive: date) -> Iterator[date]:
    """Yield every date from ``start`` up to, not including, ``end_exclusive``."""
    day = start
    while day < end_exclusive:
        yield day
        day += timedelta(days=1)


def _utc_midnight(day: date) -> datetime:
    """Timezone-aware midnight UTC for ``day`` (the exporter's time bounds)."""
    return datetime.combine(day, dt_time.min, tzinfo=UTC)


def export_day(
    day: date,
    out_dir: Path,
    *,
    max_part_mb: float = DEFAULT_WINDOW_PART_MB,
    file_prefix: str = DEFAULT_FILE_PREFIX,
    feed: Feed = EXECUTIONS,
    coins: Sequence[str] | None = None,
    include_post_only: bool = False,
) -> DayRecord:
    """Export one day in HALO-strict mode and describe the outcome.

    Never raises for a data or query problem: a DQ failure or an exception
    is captured in the returned record so the window loop can carry on
    with the next day. Configuration errors (missing credentials) still
    propagate, since every later day would fail the same way.

    Args:
        day: Transact date (UTC) to export.
        out_dir: Output directory; parts are written directly into it.
        max_part_mb: Per-part size cap in MB.
        file_prefix: Part-name prefix (production uses the tenant name).
        feed: Executions (the default) or orders.
        coins: Optional list of Allium ``COIN`` values to limit the day to.
        include_post_only: Orders only: keep the post-only (``Alo``)
            quoting rows, which the orders exporter drops by default.

    Returns:
        The :class:`DayRecord` for the day (not yet written to the ledger).
    """
    started = time.monotonic()
    remove_stale_parts(out_dir, day, file_prefix, feed)
    start_ts, end_ts = _utc_midnight(day), _utc_midnight(day + timedelta(days=1))
    coin_filter = tuple(coins) if coins else None
    try:
        if feed is ORDERS:
            orders_params = OrdersQueryParams(start_ts=start_ts, end_ts=end_ts, coin=coin_filter,
                                              exclude_post_only=not include_post_only)
            orders_result = export_orders_to_csv(
                orders_params, out_dir=out_dir, aux_filename=orders_aux_filename(day),
                halo_strict=True, max_part_mb=max_part_mb, file_prefix=file_prefix,
            )
            parts = tuple(p.name for p in orders_result.halo_paths)
            logger.info("Exported %s orders: %d rows in %d part(s); %d sized from position, "
                        "%d withheld at OrderQty 0", day, orders_result.row_count, len(parts),
                        orders_result.sized_from_position_rows, orders_result.zero_qty_rows)
            return DayRecord(day=day, status=STATUS_OK, rows=orders_result.row_count, parts=parts,
                             finished_at=_now_iso(), seconds=time.monotonic() - started)
        params = QueryParams(start_ts=start_ts, end_ts=end_ts, coin=coin_filter, halo_strict=True)
        result = export_to_csv(params, out_dir=out_dir, max_part_mb=max_part_mb,
                               file_prefix=file_prefix)
    except DqFailure as exc:
        logger.error("DQ failure for %s: %s\n%s", day, exc, exc.report.describe())
        return DayRecord(day=day, status=STATUS_DQ_FAILED, error=f"{exc}\n{exc.report.describe()}",
                         finished_at=_now_iso(), seconds=time.monotonic() - started)
    except Exception as exc:  # noqa: BLE001 - one bad day must not stop the window
        logger.exception("Export failed for %s", day)
        return DayRecord(day=day, status=STATUS_ERROR, error=f"{type(exc).__name__}: {exc}",
                         finished_at=_now_iso(), seconds=time.monotonic() - started)
    parts = tuple(p.name for p in result.halo_paths)
    logger.info("Exported %s: %d rows in %d part(s); %s", day, result.row_count, len(parts),
                result.dq_report.describe().splitlines()[0])
    return DayRecord(day=day, status=STATUS_OK, rows=result.row_count, parts=parts,
                     finished_at=_now_iso(), seconds=time.monotonic() - started)


def export_window(
    start: date,
    end_exclusive: date,
    out_dir: Path,
    *,
    max_part_mb: float = DEFAULT_WINDOW_PART_MB,
    file_prefix: str = DEFAULT_FILE_PREFIX,
    force: bool = False,
    on_day: Callable[[DayRecord], None] | None = None,
    feed: Feed = EXECUTIONS,
    coins: Sequence[str] | None = None,
    include_post_only: bool = False,
) -> WindowResult:
    """Export every day of ``[start, end_exclusive)`` and keep the ledger current.

    Args:
        start: First transact date (inclusive).
        end_exclusive: Day after the last transact date.
        out_dir: Output directory; created if missing.
        max_part_mb: Per-part size cap in MB.
        file_prefix: Part-name prefix.
        force: Re-export days whose latest ledger record is already ``ok``.
        on_day: Optional callback invoked with each day's record right
            after it is written to the ledger (the CLI uses it to print
            progress).
        feed: Executions (the default) or orders; see :func:`export_day`.
        coins: Optional list of Allium ``COIN`` values to limit every day to.
        include_post_only: Orders only: keep the post-only quoting rows.

    Returns:
        A :class:`WindowResult` listing this run's records and skipped days.

    Raises:
        ValueError: If ``end_exclusive`` is not after ``start``.
    """
    if end_exclusive <= start:
        raise ValueError(f"end_exclusive ({end_exclusive}) must be after start ({start})")
    out_dir.mkdir(parents=True, exist_ok=True)
    previous = read_status(out_dir)
    records: list[DayRecord] = []
    skipped: list[date] = []
    for day in iter_days(start, end_exclusive):
        earlier = previous.get(day)
        if earlier is not None and earlier.ok and not force:
            logger.info("Skipping %s: already exported (%d rows, %d part(s))",
                        day, earlier.rows, len(earlier.parts))
            skipped.append(day)
            continue
        record = export_day(day, out_dir, max_part_mb=max_part_mb, file_prefix=file_prefix,
                            feed=feed, coins=coins, include_post_only=include_post_only)
        append_status(out_dir, record)
        records.append(record)
        if on_day is not None:
            on_day(record)
    return WindowResult(records=tuple(records), skipped=tuple(skipped))


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string with second precision."""
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat()
