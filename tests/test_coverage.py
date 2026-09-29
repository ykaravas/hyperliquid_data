"""Unit tests for :mod:`hyperliquid_halo.coverage` (no ClickHouse access).

The clickhouse-download skill is replaced by a fake ``subprocess.run`` that
writes a canned CSV (with CRLF line endings, as the real skill does on this
host) to the ``--out`` path the module asked for.
"""

from __future__ import annotations

import re
import subprocess
from datetime import date
from pathlib import Path

import pytest

from hyperliquid_halo import coverage

STRICT_CSV = (
    "day,rows,distinct_ids\r\n"
    "2026-05-19,8669407,8669407\r\n"
    "2026-05-21,10467798,10368004\r\n"
)
RAW_CSV = (
    "day,orig_file,rows,first_loaded,last_loaded\r\n"
    "2026-05-19,date=2026-05-20/sdny_LINKED_PRIVATE_EXECUTION_V2_19052026_part1.csv,300000,"
    "2026-05-20 01:00:00,2026-05-20 01:00:00\r\n"
    "2026-05-19,date=2026-05-19/hyper manual test - Sheet1.csv,5,"
    "2026-05-19 12:00:00,2026-05-19 12:00:00\r\n"
    "2026-05-21,date=2026-05-22/sdny_LINKED_PRIVATE_EXECUTION_V2_21052026_part1.csv,10467798,"
    "2026-05-22 01:00:00,2026-05-23 01:00:00\r\n"
)


class _FakeRun:
    """Writes the canned CSV matching the query's table to the requested ``--out``."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.queries: list[str] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        sql = command[command.index("--query") + 1]
        self.queries.append(sql)
        if self.returncode == 0:
            body = STRICT_CSV if coverage.STRICT_EVENTS_TABLE in sql else RAW_CSV
            Path(command[command.index("--out") + 1]).write_bytes(body.encode())
        return subprocess.CompletedProcess(command, self.returncode, stdout="",
                                           stderr="boom\nconnection reset")


def _settings(tmp_path: Path) -> coverage.CoverageSettings:
    script = tmp_path / "script.py"
    script.write_text("")
    return coverage.CoverageSettings(script=script, python="python3")


def test_sql_names_tenant_and_inclusive_dates() -> None:
    """Both queries filter on the tenant and the inclusive last day of the range."""
    strict = coverage.strict_events_sql("hlresearch", date(2026, 5, 17), date(2026, 5, 22))
    raw = coverage.raw_files_sql("HLRESEARCH", date(2026, 5, 17), date(2026, 5, 22))
    assert "exchange = 'HLRESEARCH'" in strict and "event_type = 'EXECUTION'" in strict
    assert "BETWEEN '2026-05-17' AND '2026-05-21'" in strict
    assert "solidus_client = 'HLRESEARCH'" in raw and "orig_file" in raw
    assert strict.upper().startswith("SELECT") and raw.upper().startswith("SELECT")


def test_unsafe_tenant_is_refused() -> None:
    """Only ``[A-Z0-9_]`` tenants are spliced into SQL."""
    with pytest.raises(coverage.CoverageError, match="Tenant must match"):
        coverage.strict_events_sql("x' OR 1=1 --", date(2026, 5, 1), date(2026, 5, 2))


def test_check_coverage_reads_crlf_csv_and_fills_empty_days(tmp_path: Path) -> None:
    """Days without rows appear with zeros; covered days carry their source files."""
    run = _FakeRun()
    report = coverage.check_coverage("HLRESEARCH", date(2026, 5, 19), date(2026, 5, 22),
                                     _settings(tmp_path), run=run)
    assert [d.day for d in report.days] == [date(2026, 5, 19), date(2026, 5, 20), date(2026, 5, 21)]
    may19, may20, may21 = report.days
    assert may19.covered and may19.rows == 8_669_407 and len(may19.sources) == 2
    assert may19.sources[1].orig_file == "date=2026-05-19/hyper manual test - Sheet1.csv"
    assert not may20.covered and may20.sources == ()
    assert may21.distinct_ids == 10_368_004
    assert may21.sources[0].last_loaded == "2026-05-23 01:00:00"
    assert [d.day for d in report.covered_days] == [date(2026, 5, 19), date(2026, 5, 21)]
    assert len(run.queries) == 2


def test_blocked_days_honor_the_allow_list() -> None:
    """Covered days are blocked unless explicitly allowed."""
    report = coverage.CoverageReport("T", (
        coverage.DayCoverage(date(2026, 5, 17), rows=10),
        coverage.DayCoverage(date(2026, 5, 18), rows=0),
        coverage.DayCoverage(date(2026, 5, 19), rows=5),
    ))
    assert report.blocked_days() == (date(2026, 5, 17), date(2026, 5, 19))
    assert report.blocked_days([date(2026, 5, 17)]) == (date(2026, 5, 19),)


def test_failed_query_raises_with_the_skill_error(tmp_path: Path) -> None:
    """A non-zero skill exit surfaces the tail of its stderr."""
    with pytest.raises(coverage.CoverageError, match="rc=1.*connection reset"):
        coverage.check_coverage("HLRESEARCH", date(2026, 5, 19), date(2026, 5, 20),
                                _settings(tmp_path), run=_FakeRun(returncode=1))


def test_format_report_lists_sources_and_elides(tmp_path: Path) -> None:
    """The table shows every day, marks covered ones and caps the source listing."""
    report = coverage.check_coverage("HLRESEARCH", date(2026, 5, 19), date(2026, 5, 22),
                                     _settings(tmp_path), run=_FakeRun())
    text = coverage.format_report(report, max_sources=1)
    assert "2 of 3 day(s) already hold rows" in text
    assert re.search(r"^2026-05-20\s+0\s+0\s+0  empty$", text, re.MULTILINE)
    assert "COVERED" in text and "... 1 more file(s)" in text
    assert "hyper manual test" not in text, "second source elided at max_sources=1"


def test_short_source_strips_the_tenant_prefix() -> None:
    """Only the ``date=.../file`` tail of an S3 source key is shown."""
    key = ("solidusClient=HLRESEARCH/fileType=linked_private_execution_v2/"
           "date=2026-09-24/sdny_LINKED_PRIVATE_EXECUTION_V2_17052026_part1.csv_20260924214658879")
    assert coverage.short_source(key) == (
        "date=2026-09-24/sdny_LINKED_PRIVATE_EXECUTION_V2_17052026_part1.csv_20260924214658879"
    )
    assert coverage.short_source("date=2026-05-19/manual.csv") == "date=2026-05-19/manual.csv"
    assert coverage.short_source("no_prefix.csv") == "no_prefix.csv"


def test_resolve_settings_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit values beat environment variables, which beat the defaults."""
    explicit, from_env = tmp_path / "a.py", tmp_path / "b.py"
    explicit.write_text(""), from_env.write_text("")
    monkeypatch.setenv(coverage.SCRIPT_ENV, str(from_env))
    monkeypatch.setenv(coverage.PYTHON_ENV, "/opt/py")
    assert coverage.resolve_settings(explicit).script == explicit
    settings = coverage.resolve_settings()
    assert settings.script == from_env and settings.python == "/opt/py"
    assert settings.ch_env == "uat-eu" and settings.database == "solidus_uat_eu"
    monkeypatch.setenv(coverage.SCRIPT_ENV, str(tmp_path / "missing.py"))
    with pytest.raises(coverage.CoverageError, match="not found"):
        coverage.resolve_settings()
