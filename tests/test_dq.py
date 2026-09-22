"""Unit tests for :mod:`hyperliquid_halo.dq`, the streaming mirror of
production's ``hyperliquid_sp_run_dq`` (DQ-1..DQ-9).

Rows are fed in the exporter's query order ``(TransactTime, _SourceTradeId,
Side)``; no Snowflake connection is involved.
"""

from __future__ import annotations

from typing import Any

import pytest

from hyperliquid_halo.dq import (
    ALLOWED_DIRS,
    FLIP_DIRS,
    REQUIRED_FIELDS,
    DqFailure,
    ExecutionDqChecker,
)


def _side(
    trade: str,
    side: str,
    *,
    symbol: str = "BTC/USDC",
    security_type: str = "SWAP",
    position_effect: str | None = "OPEN",
    symbol_type: str | None = "Crypto",
    direction: str | None = "Open Long",
    is_hip3: bool | None = False,
    **halo_overrides: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build one (halo_row, aux_row) pair with every required field populated."""
    halo: dict[str, Any] = {
        "TransactTime": 1, "Id": f"{trade}-{side[0]}", "MatchingID": f"{trade}-X",
        "OrderID": "1", "Side": side, "Quantity": "1", "Price": "1", "Symbol": symbol,
        "ExVenue": "Hyperliquid", "SecurityType": security_type,
        "PositionEffect": position_effect, "SymbolType": symbol_type,
    }
    halo.update(halo_overrides)
    aux = {"_SourceTradeId": trade, "_Direction": direction, "_IsHip3": is_hip3}
    return halo, aux


def _trade(trade: str, **kw: Any) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """A well-formed Buy + Sell pair for one trade."""
    return [_side(trade, "Buy", **kw), _side(trade, "Sell", **kw)]


def _run(rows: list[tuple[dict[str, Any], dict[str, Any]]]) -> Any:
    checker = ExecutionDqChecker()
    for halo, aux in rows:
        checker.observe(halo, aux)
    return checker.finish()


def _status(report: Any, check: str) -> str:
    return next(r.status for r in report.results if r.check == check)


def test_clean_batch_passes_every_check() -> None:
    rows = (
        _trade("t1")
        + _trade("t2", symbol="HYPE/USDC", security_type="SPOT", position_effect=None,
                 direction="Buy")
        + _trade("t3", symbol="TSLA-XYZ/USDC", symbol_type="Equity", is_hip3=True)
        + _trade("t4", position_effect=None, direction="Long > Short")
    )
    report = _run(rows)
    assert report.ok
    assert len(report.results) == 9
    assert report.passes == tuple(f"DQ-{i}" for i in range(1, 10))
    assert report.warnings == ()
    dq1 = next(r for r in report.results if r.check == "DQ-1")
    assert dq1.observed == {"rows": 8, "trades": 4, "expected": 8}
    assert "9 of 9 checks passed" in report.describe()


def test_dq1_and_dq2_catch_an_unpaired_side() -> None:
    rows = _trade("t1") + [_side("t2", "Buy")]  # t2 has no Sell
    report = _run(rows)
    assert _status(report, "DQ-1") == "FAIL"
    assert _status(report, "DQ-2") == "FAIL"
    assert not report.ok


def test_dq2_catches_two_buys() -> None:
    rows = [_side("t1", "Buy"), _side("t1", "Buy")]
    report = _run(rows)
    assert _status(report, "DQ-1") == "PASS"  # 2 rows, 1 trade
    assert _status(report, "DQ-2") == "FAIL"


def test_dq3_counts_nulls_per_required_field() -> None:
    rows = _trade("t1", Price=None) + _trade("t2", Quantity="")
    report = _run(rows)
    dq3 = next(r for r in report.results if r.check == "DQ-3")
    assert dq3.status == "FAIL"
    assert dq3.observed["null_counts"]["Price"] == 2
    assert dq3.observed["null_counts"]["Quantity"] == 2
    assert dq3.observed["total_nulls"] == 4
    assert set(dq3.observed["null_counts"]) == set(REQUIRED_FIELDS)


def test_dq4_and_dq5() -> None:
    rows = _trade("t1", security_type="FUT") + _trade("t2", symbol="X" * 42)
    report = _run(rows)
    assert _status(report, "DQ-4") == "FAIL"
    dq5 = next(r for r in report.results if r.check == "DQ-5")
    assert dq5.status == "FAIL"
    assert dq5.observed == {"violations": 2, "max_observed_length": 42}


def test_dq6_allows_null_position_effect_only_on_spot_or_flips() -> None:
    ok = _trade("t1", security_type="SPOT", position_effect=None, direction="Buy") + _trade(
        "t2", position_effect=None, direction=FLIP_DIRS[0]
    )
    assert _status(_run(ok), "DQ-6") == "PASS"
    bad = _trade("t3", position_effect=None, direction="Open Long")
    report = _run(bad)
    assert _status(report, "DQ-6") == "FAIL"


def test_dq7_fails_on_unknown_or_null_direction() -> None:
    rows = _trade("t1", direction="Liquidated Cross Long") + _trade("t2", direction=None)
    report = _run(rows)
    dq7 = next(r for r in report.results if r.check == "DQ-7")
    assert dq7.status == "FAIL"
    assert dq7.observed["violations"] == 4
    assert dq7.observed["unexpected_dirs"] == ["Liquidated Cross Long", "None"]
    assert set(ALLOWED_DIRS) >= {"Open Long", "Settlement", "Buy", "Sell", *FLIP_DIRS}


def test_dq8_pattern_and_symmetry() -> None:
    pattern_bad = _trade("t1", symbol="TSLA/USDC", is_hip3=True)
    assert _status(_run(pattern_bad), "DQ-8") == "FAIL"
    symmetry_bad = [_side("t2", "Buy", symbol="BTC/USDC"), _side("t2", "Sell", symbol="ETH/USDC")]
    report = _run(symmetry_bad)
    dq8 = next(r for r in report.results if r.check == "DQ-8")
    assert dq8.status == "FAIL"
    assert dq8.observed == {"pattern_violations": 0, "symmetry_violations": 1}


def test_dq9_warns_without_failing_and_names_symbols() -> None:
    rows = (
        _trade("t1", symbol="PONS/USDC", symbol_type=None)
        + _trade("t2", symbol="PONS/USDC", symbol_type=None)
        + _trade("t3", symbol="NEWCOIN/USDC", symbol_type="")
        + _trade("t4", symbol="HYPE/USDC", security_type="SPOT", position_effect=None,
                 symbol_type=None, direction="Buy")  # spot never counts
    )
    report = _run(rows)
    assert report.ok, "DQ-9 must not block"
    dq9 = next(r for r in report.results if r.check == "DQ-9")
    assert dq9.status == "WARN"
    assert dq9.observed["rows"] == 6 and dq9.observed["trades"] == 3
    assert dq9.observed["top_symbols"][0] == {"symbol": "PONS/USDC", "rows": 4}
    assert "PONS/USDC 4" in (dq9.summary or "")
    assert "sync_symbol_type_map" in (dq9.summary or "")
    assert "WARN DQ-9" in report.describe()


def test_observe_after_finish_is_rejected() -> None:
    checker = ExecutionDqChecker()
    checker.finish()
    with pytest.raises(RuntimeError, match="after finish"):
        checker.observe(*_side("t1", "Buy"))


def test_dq_failure_message_lists_failing_checks() -> None:
    report = _run([_side("t1", "Buy", security_type="FUT")])
    exc = DqFailure(report)
    assert "DQ-1" in str(exc) and "DQ-4" in str(exc)
    assert exc.report is report
