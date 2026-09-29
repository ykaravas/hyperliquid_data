"""Unit tests for :mod:`hyperliquid_halo.window` (no Snowflake connection).

``export_to_csv`` is monkeypatched with a fake that writes empty part files
and returns an :class:`ExportResult`, so the tests exercise the per-day loop,
the ledger and the stale-part cleanup only.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from hyperliquid_halo import window
from hyperliquid_halo.dq import DqFailure, DqReport, DqResult, ExecutionDqChecker
from hyperliquid_halo.exporter import ExportResult
from hyperliquid_halo.mapping import QueryParams


def _ok_report() -> DqReport:
    """A DQ report with every check passing (no rows observed)."""
    return ExecutionDqChecker().finish()


def _failed_report() -> DqReport:
    """A DQ report with one failing check."""
    return DqReport(results=(DqResult("DQ-1", "one side per trade", "FAIL", {"bad": 1}),))


def _fake_export(parts_per_day: dict[date, int], rows: int = 10) -> Any:
    """Build an ``export_to_csv`` double that writes ``parts_per_day[day]`` empty parts."""

    def fake(params: QueryParams, out_dir: Path, *, max_part_mb: float | None = None,
             file_prefix: str = "sdny", **_: Any) -> ExportResult:
        day = params.start_ts.date()
        assert params.halo_strict, "window exports must be HALO-strict"
        assert params.end_ts.date() == day.replace(day=day.day + 1)
        (out_dir / "aux").mkdir(exist_ok=True)
        halo_paths: list[Path] = []
        aux_paths: list[Path] = []
        for number in range(1, parts_per_day[day] + 1):
            name = f"{file_prefix}_LINKED_PRIVATE_EXECUTION_V2_{day:%d%m%Y}_part{number}.csv"
            halo, aux = out_dir / name, out_dir / "aux" / name
            halo.write_text("halo"), aux.write_text("aux")
            halo_paths.append(halo), aux_paths.append(aux)
        return ExportResult(halo_path=halo_paths[0], aux_path=aux_paths[0], row_count=rows,
                            dq_report=_ok_report(), halo_paths=tuple(halo_paths),
                            aux_paths=tuple(aux_paths))

    return fake


def test_export_window_records_each_day_with_its_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every day gets one ``ok`` record naming its parts in part order."""
    monkeypatch.setattr(window, "export_to_csv",
                        _fake_export({date(2026, 5, 5): 2, date(2026, 5, 6): 11}))
    seen: list[window.DayRecord] = []
    result = window.export_window(date(2026, 5, 5), date(2026, 5, 7), tmp_path, on_day=seen.append)

    assert result.ok and not result.skipped
    assert [r.day for r in result.records] == [date(2026, 5, 5), date(2026, 5, 6)]
    assert seen == list(result.records)
    ledger = window.read_status(tmp_path)
    assert ledger[date(2026, 5, 5)].parts == (
        "sdny_LINKED_PRIVATE_EXECUTION_V2_05052026_part1.csv",
        "sdny_LINKED_PRIVATE_EXECUTION_V2_05052026_part2.csv",
    )
    # part10 and part11 sort after part9 (numeric, not lexical).
    parts_may6 = ledger[date(2026, 5, 6)].parts
    assert parts_may6[-2:] == (
        "sdny_LINKED_PRIVATE_EXECUTION_V2_06052026_part10.csv",
        "sdny_LINKED_PRIVATE_EXECUTION_V2_06052026_part11.csv",
    )
    assert ledger[date(2026, 5, 6)].rows == 10 and ledger[date(2026, 5, 6)].finished_at


def test_rerun_skips_ok_days_unless_forced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second run over the same window exports nothing; ``force`` re-exports."""
    calls: list[date] = []
    fake = _fake_export({date(2026, 5, 5): 1})

    def counting(params: QueryParams, out_dir: Path, **kwargs: Any) -> ExportResult:
        calls.append(params.start_ts.date())
        return fake(params, out_dir, **kwargs)

    monkeypatch.setattr(window, "export_to_csv", counting)
    window.export_window(date(2026, 5, 5), date(2026, 5, 6), tmp_path)
    second = window.export_window(date(2026, 5, 5), date(2026, 5, 6), tmp_path)
    assert second.records == () and second.skipped == (date(2026, 5, 5),)
    assert calls == [date(2026, 5, 5)]

    forced = window.export_window(date(2026, 5, 5), date(2026, 5, 6), tmp_path, force=True)
    assert len(forced.records) == 1 and calls == [date(2026, 5, 5)] * 2


def test_dq_failure_is_recorded_and_the_loop_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DQ failure on one day yields a ``dq_failed`` record; the next day still runs."""
    good = _fake_export({date(2026, 5, 6): 1})

    def flaky(params: QueryParams, out_dir: Path, **kwargs: Any) -> ExportResult:
        if params.start_ts.date() == date(2026, 5, 5):
            raise DqFailure(_failed_report())
        return good(params, out_dir, **kwargs)

    monkeypatch.setattr(window, "export_to_csv", flaky)
    result = window.export_window(date(2026, 5, 5), date(2026, 5, 7), tmp_path)
    assert not result.ok
    bad, ok = result.records
    assert bad.status == window.STATUS_DQ_FAILED and "DQ-1" in bad.error and bad.parts == ()
    assert ok.ok
    # The latest record per day is what a re-run sees: the failed day is retried.
    assert not window.read_status(tmp_path)[date(2026, 5, 5)].ok


def test_unexpected_exception_is_recorded_as_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any other exception becomes an ``error`` record instead of aborting the window."""

    def boom(params: QueryParams, out_dir: Path, **kwargs: Any) -> ExportResult:
        raise ConnectionError("snowflake went away")

    monkeypatch.setattr(window, "export_to_csv", boom)
    result = window.export_window(date(2026, 5, 5), date(2026, 5, 6), tmp_path)
    (record,) = result.records
    assert record.status == window.STATUS_ERROR
    assert record.error == "ConnectionError: snowflake went away"


def test_stale_parts_are_removed_before_a_day_is_re_exported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parts left by an earlier attempt (HALO and aux) never survive a re-export."""
    (tmp_path / "aux").mkdir()
    stale = "sdny_LINKED_PRIVATE_EXECUTION_V2_05052026_part7.csv"
    (tmp_path / stale).write_text("old")
    (tmp_path / "aux" / stale).write_text("old")
    other_day = "sdny_LINKED_PRIVATE_EXECUTION_V2_06052026_part1.csv"
    (tmp_path / other_day).write_text("keep")

    monkeypatch.setattr(window, "export_to_csv", _fake_export({date(2026, 5, 5): 1}))
    window.export_window(date(2026, 5, 5), date(2026, 5, 6), tmp_path)

    assert not (tmp_path / stale).exists() and not (tmp_path / "aux" / stale).exists()
    assert (tmp_path / other_day).exists(), "parts of other days are untouched"
    assert [p.name for p in window.day_parts(tmp_path, date(2026, 5, 5))] == [
        "sdny_LINKED_PRIVATE_EXECUTION_V2_05052026_part1.csv"
    ]


def test_ledger_round_trip_and_latest_record_wins(tmp_path: Path) -> None:
    """Records survive JSON serialization; a later line for the same day replaces the earlier."""
    first = window.DayRecord(day=date(2026, 5, 5), status=window.STATUS_ERROR, error="x")
    second = window.DayRecord(day=date(2026, 5, 5), status=window.STATUS_OK, rows=3,
                              parts=("a_part1.csv",), finished_at="2026-09-25T10:00:00+00:00",
                              seconds=1.5)
    window.append_status(tmp_path, first)
    window.append_status(tmp_path, second)
    assert window.read_status(tmp_path) == {date(2026, 5, 5): second}
    assert window.DayRecord.from_json(second.to_json()) == second


def test_malformed_ledger_line_is_an_error(tmp_path: Path) -> None:
    """A hand-edited, unparsable ledger is refused rather than guessed at."""
    window.status_path(tmp_path).write_text("not json\n")
    with pytest.raises(ValueError, match="Malformed ledger line"):
        window.read_status(tmp_path)


def test_part_number_and_invalid_range() -> None:
    """Part numbers parse numerically; an empty window is rejected."""
    assert window.part_number("sdny_LINKED_PRIVATE_EXECUTION_V2_05052026_part12.csv") == 12
    with pytest.raises(ValueError):
        window.part_number("halo.csv")
    with pytest.raises(ValueError, match="must be after"):
        window.export_window(date(2026, 5, 5), date(2026, 5, 5), Path("unused"))
