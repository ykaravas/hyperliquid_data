"""What a HALO tenant already holds for a date range, read from ClickHouse.

The upload ledger in ``docs/HALO_UPLOAD_LEDGER.md`` only knows about uploads
made from this repo. The tenant's daily pipeline, manual tests and other
people's backfills are invisible to it, which is how three days were
double-counted in September 2026. So before a window is exported the
tenant itself is asked, per day and per source file:

* ``strict_events`` gives rows and distinct execution ids per transact
  date (``exchange = <tenant>``, ``event_type = 'EXECUTION'``).
* ``raw_realtime_matched_executions`` gives the source files
  (``orig_file``) behind those rows, with row counts and load times, so a
  ten-row manual test can be told apart from a full day's load.

Queries run through the read-only ``clickhouse-download`` skill
(``CLICKHOUSE_DOWNLOAD_SCRIPT``, default
``~/.claude/skills/clickhouse-download/script.py``) so the ClickHouse
credentials stay in that skill's ``.env``. The skill needs an interpreter
with ``clickhouse-connect`` installed (``CLICKHOUSE_DOWNLOAD_PYTHON``,
default ``python3``). Both are known portability hazards, documented in
the README.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .window import iter_days

logger = logging.getLogger(__name__)

SCRIPT_ENV = "CLICKHOUSE_DOWNLOAD_SCRIPT"
"""Environment variable overriding the clickhouse-download skill script path."""

PYTHON_ENV = "CLICKHOUSE_DOWNLOAD_PYTHON"
"""Environment variable naming the interpreter that runs the skill."""

DEFAULT_SCRIPT = "~/.claude/skills/clickhouse-download/script.py"
DEFAULT_PYTHON = "python3"
DEFAULT_CH_ENV = "uat-eu"
"""Named environment in the skill's ``.env`` (``CH_UAT_EU_*``) for the research tenants."""

DEFAULT_CH_DATABASE = "solidus_uat_eu"

STRICT_EVENTS_TABLE = "strict_events"
RAW_EXECUTIONS_TABLE = "raw_realtime_matched_executions"

_TENANT_RE = re.compile(r"^[A-Z0-9_]+$")
RunFn = Callable[..., subprocess.CompletedProcess[str]]


class CoverageError(RuntimeError):
    """Raised when the tenant could not be queried or gave an unusable answer."""


@dataclass(frozen=True)
class CoverageSettings:
    """How to reach ClickHouse through the clickhouse-download skill.

    Attributes:
        script: Path of the skill's ``script.py``.
        python: Interpreter to run it with (must have ``clickhouse-connect``).
        ch_env: The skill's named environment (``--env``).
        database: ClickHouse database (``--database``).
    """

    script: Path
    python: str = DEFAULT_PYTHON
    ch_env: str = DEFAULT_CH_ENV
    database: str = DEFAULT_CH_DATABASE


@dataclass(frozen=True)
class SourceFile:
    """Rows one source file contributed to a day of the raw table."""

    orig_file: str
    rows: int
    first_loaded: str
    last_loaded: str


@dataclass(frozen=True)
class DayCoverage:
    """What the tenant holds for one transact date.

    Attributes:
        day: Transact date (UTC).
        rows: Rows in ``strict_events``.
        distinct_ids: Distinct execution ids among those rows.
        sources: Source files in the raw table for the day.
    """

    day: date
    rows: int = 0
    distinct_ids: int = 0
    sources: tuple[SourceFile, ...] = ()

    @property
    def covered(self) -> bool:
        """``True`` when the tenant already holds any row for the day."""
        return self.rows > 0


@dataclass(frozen=True)
class CoverageReport:
    """Coverage of every day in the requested range, in date order."""

    tenant: str
    days: tuple[DayCoverage, ...]

    @property
    def covered_days(self) -> tuple[DayCoverage, ...]:
        """Days that already hold rows on the tenant."""
        return tuple(d for d in self.days if d.covered)

    def blocked_days(self, allowed: Sequence[date] = ()) -> tuple[date, ...]:
        """Covered days that were not explicitly allowed for export."""
        allowed_set = set(allowed)
        return tuple(d.day for d in self.covered_days if d.day not in allowed_set)


def resolve_settings(
    script: Path | None = None,
    python: str | None = None,
    ch_env: str = DEFAULT_CH_ENV,
    database: str = DEFAULT_CH_DATABASE,
) -> CoverageSettings:
    """Resolve the skill location from arguments, environment, then defaults.

    Args:
        script: Explicit script path (wins over :data:`SCRIPT_ENV`).
        python: Explicit interpreter (wins over :data:`PYTHON_ENV`).
        ch_env: Skill named environment.
        database: ClickHouse database.

    Returns:
        The settings, with ``~`` expanded.

    Raises:
        CoverageError: If the script does not exist.
    """
    raw_script = script or Path(os.environ.get(SCRIPT_ENV, "").strip() or DEFAULT_SCRIPT)
    script_path = raw_script.expanduser()
    if not script_path.is_file():
        raise CoverageError(
            f"clickhouse-download skill script not found at {script_path}. Install the skill "
            f"or set {SCRIPT_ENV} (or --ch-script) to its script.py."
        )
    interpreter = python or os.environ.get(PYTHON_ENV, "").strip() or DEFAULT_PYTHON
    return CoverageSettings(script=script_path, python=interpreter, ch_env=ch_env,
                            database=database)


def _validate_tenant(tenant: str) -> str:
    """Return ``tenant`` upper-cased, refusing anything unsafe to splice into SQL."""
    cleaned = tenant.strip().upper()
    if not _TENANT_RE.match(cleaned):
        raise CoverageError(f"Tenant must match {_TENANT_RE.pattern}, got {tenant!r}")
    return cleaned


def strict_events_sql(tenant: str, start: date, end_exclusive: date) -> str:
    """Rows and distinct ids per transact date in ``strict_events``."""
    last = end_exclusive - timedelta(days=1)
    return (
        "SELECT toDate(ts) AS day, count() AS rows, uniqExact(id) AS distinct_ids "
        f"FROM {STRICT_EVENTS_TABLE} "
        f"WHERE exchange = '{_validate_tenant(tenant)}' AND event_type = 'EXECUTION' "
        f"AND toDate(ts) BETWEEN '{start.isoformat()}' AND '{last.isoformat()}' "
        "GROUP BY day ORDER BY day"
    )


def raw_files_sql(tenant: str, start: date, end_exclusive: date) -> str:
    """Rows per (transact date, source file) in the raw executions table."""
    last = end_exclusive - timedelta(days=1)
    return (
        "SELECT toDate(transact_time) AS day, orig_file, count() AS rows, "
        "min(data_loaded_at) AS first_loaded, max(data_loaded_at) AS last_loaded "
        f"FROM {RAW_EXECUTIONS_TABLE} "
        f"WHERE solidus_client = '{_validate_tenant(tenant)}' "
        f"AND toDate(transact_time) BETWEEN '{start.isoformat()}' AND '{last.isoformat()}' "
        "GROUP BY day, orig_file ORDER BY day, orig_file"
    )


def run_query(
    sql: str, settings: CoverageSettings, *, run: RunFn = subprocess.run
) -> list[dict[str, str]]:
    """Run a read-only query through the skill and return its rows.

    The skill writes a CSV (with CRLF line endings on some hosts); it is
    read back with the ``csv`` module, which normalizes those.

    Args:
        sql: The SELECT to run.
        settings: Skill location and environment.
        run: ``subprocess.run``-compatible callable (injectable for tests).

    Returns:
        One dict per row, keyed by the column aliases in ``sql``.

    Raises:
        CoverageError: If the skill exits non-zero or writes no file.
    """
    with tempfile.TemporaryDirectory(prefix="hl_coverage_") as tmp:
        out = Path(tmp) / "result.csv"
        command = [
            settings.python, str(settings.script),
            "--env", settings.ch_env,
            "--database", settings.database,
            "--out", str(out),
            "--query", sql,
        ]
        completed = run(command, capture_output=True, text=True)
        if completed.returncode != 0 or not out.exists():
            tail = (completed.stderr or completed.stdout).strip().splitlines()[-3:]
            raise CoverageError(
                f"ClickHouse query failed (rc={completed.returncode}): {' | '.join(tail)}"
            )
        with out.open(newline="", encoding="utf-8") as fh:
            return [dict(row) for row in csv.DictReader(fh)]


def check_coverage(
    tenant: str,
    start: date,
    end_exclusive: date,
    settings: CoverageSettings,
    *,
    run: RunFn = subprocess.run,
) -> CoverageReport:
    """Ask the tenant what it holds for every day of ``[start, end_exclusive)``.

    Args:
        tenant: HALO tenant name (``exchange`` / ``solidus_client`` value).
        start: First transact date (inclusive).
        end_exclusive: Day after the last transact date.
        settings: Skill location and environment.
        run: ``subprocess.run``-compatible callable (injectable for tests).

    Returns:
        A :class:`CoverageReport` with one entry per requested day, days
        without rows included with zeros.

    Raises:
        CoverageError: On an invalid tenant or a failed query.
    """
    if end_exclusive <= start:
        raise CoverageError(f"end_exclusive ({end_exclusive}) must be after start ({start})")
    tenant = _validate_tenant(tenant)
    per_day: dict[date, dict[str, int]] = {}
    for row in run_query(strict_events_sql(tenant, start, end_exclusive), settings, run=run):
        per_day[date.fromisoformat(row["day"])] = {
            "rows": int(row["rows"]), "distinct_ids": int(row["distinct_ids"]),
        }
    sources: dict[date, list[SourceFile]] = {}
    for row in run_query(raw_files_sql(tenant, start, end_exclusive), settings, run=run):
        sources.setdefault(date.fromisoformat(row["day"]), []).append(
            SourceFile(orig_file=row["orig_file"], rows=int(row["rows"]),
                       first_loaded=row["first_loaded"], last_loaded=row["last_loaded"])
        )
    days = tuple(
        DayCoverage(
            day=day,
            rows=per_day.get(day, {}).get("rows", 0),
            distinct_ids=per_day.get(day, {}).get("distinct_ids", 0),
            sources=tuple(sources.get(day, ())),
        )
        for day in iter_days(start, end_exclusive)
    )
    return CoverageReport(tenant=tenant, days=days)


def short_source(orig_file: str) -> str:
    """Drop the constant ``solidusClient=.../fileType=.../`` prefix of an S3 source key.

    The upload ledger names source files the same way (from ``date=`` on),
    so the table and the ledger can be compared by eye.
    """
    marker = "/date="
    index = orig_file.find(marker)
    return orig_file[index + 1:] if index >= 0 else orig_file


def format_report(report: CoverageReport, *, max_sources: int = 5) -> str:
    """Render a report as a fixed-width table with a source-file breakdown.

    Args:
        report: The coverage to render.
        max_sources: Source files listed per covered day before eliding.

    Returns:
        Multi-line text for the terminal.
    """
    lines = [f"Tenant {report.tenant}: {len(report.covered_days)} of {len(report.days)} day(s) "
             "already hold rows",
             f"{'day':<12}{'rows':>14}{'distinct_ids':>14}{'files':>7}  status"]
    for day in report.days:
        status = "COVERED" if day.covered else "empty"
        lines.append(f"{day.day.isoformat():<12}{day.rows:>14,}{day.distinct_ids:>14,}"
                     f"{len(day.sources):>7}  {status}")
        for source in day.sources[:max_sources]:
            lines.append(f"    {source.rows:>12,}  {short_source(source.orig_file)}  "
                         f"(loaded {source.first_loaded} .. {source.last_loaded})")
        if len(day.sources) > max_sources:
            lines.append(f"    ... {len(day.sources) - max_sources} more file(s)")
    return "\n".join(lines)
