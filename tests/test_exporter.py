"""Unit tests for :mod:`hyperliquid_halo.exporter` using a fake cursor.

No Snowflake connection is opened — we monkeypatch the ``cursor`` context
manager to yield a dummy cursor that replays canned rows.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from hyperliquid_halo import exporter
from hyperliquid_halo.dq import DqFailure
from hyperliquid_halo.mapping import (
    ALL_COLUMNS,
    AUX_COLUMNS,
    HALO_COLUMNS,
    HALO_STRICT_COLUMNS,
    QueryParams,
)


class _FakeCursor:
    """Minimal cursor double that returns preset rows once ``execute`` is called."""

    def __init__(self, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
        self._columns = columns
        self._rows = rows
        self._executed = False
        self.description: list[tuple[str, ...]] = [(c,) for c in columns]

    def execute(self, sql: str, binds: dict[str, Any]) -> None:
        self._executed = True
        self.last_sql = sql
        self.last_binds = binds

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        if not self._executed:
            return []
        batch, self._rows = self._rows[:size], self._rows[size:]
        return batch

    def close(self) -> None:
        pass


def _make_row(**overrides: Any) -> tuple[Any, ...]:
    """Build a fake source row covering every column in ``ALL_COLUMNS``."""
    defaults: dict[str, Any] = {col: None for col in ALL_COLUMNS}
    defaults.update({
        "TransactTime": 1_712_000_000_000,
        "Id": "tx1-B",
        "MatchingID": "tx1-S",
        "OrderID": "1001",
        "MatchingOrderID": "1002",
        "ExecutionType": "EXCHANGE",
        "Symbol": "BTC/USDC",
        "Side": "Buy",
        "Quantity": "0.5",
        "Price": "70000",
        "Notional": "35000",
        "Status": "Filled",
        "ExVenue": "Hyperliquid",
        "Account": "0xbuy",
        "MatchingAccount": "0xsell",
        "ClientId": "0xbuy",
        "MatchingClientId": "0xsell",
        "OriginationTrader": "0xbuy",
        "MatchingOriginationTrader": "0xsell",
        "OrderCapacity": "Agency",
        "MatchingOrderCapacity": "Agency",
        "TrdType": "RegularTrade",
        "Blockchain": "ethereum",
        "WalletAddress": "0xbuy",
        "SecurityType": "SWAP",
        "ExchangeSymbol": "Hyperliquid:BTC",
        "PositionEffect": "OPEN",
        "ContractMultiplier": "1",
        "SymbolType": "Crypto",
        "_SourceTradeId": "tx1",
        "_TradeId": "9001",
        "_MarketType": "perpetuals",
        "_Coin": "BTC",
        "_Direction": "Open Long",
        "_IsHip3": False,
        "IsMaker": False,
        "_IsTaker": True,
    })
    defaults.update(overrides)
    return tuple(defaults[col] for col in ALL_COLUMNS)


@pytest.fixture()
def fake_rows() -> list[tuple[Any, ...]]:
    return [
        _make_row(),
        _make_row(Id="tx1-S", MatchingID="tx1-B", Side="Sell",
                  Account="0xsell", MatchingAccount="0xbuy"),
        _make_row(Id="tx2-B", MatchingID="tx2-S", Symbol="HYPE/USDC",
                  SecurityType="SPOT", PositionEffect=None, ContractMultiplier=None,
                  SymbolType="Crypto", _SourceTradeId="tx2", _MarketType="spot", _Coin="@4",
                  _Direction="Buy"),
        # Perp missing from the SymbolType map: SymbolType stays empty, never guessed.
        _make_row(Id="tx3-B", MatchingID="tx3-S", Symbol="PONS/USDC", SymbolType=None,
                  _SourceTradeId="tx3", _Coin="PONS"),
    ]


def test_export_to_csv_writes_both_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_rows: list[tuple[Any, ...]],
) -> None:
    fake = _FakeCursor(columns=list(ALL_COLUMNS), rows=fake_rows)

    @contextmanager
    def _fake_cursor() -> Iterator[_FakeCursor]:
        yield fake

    monkeypatch.setattr(exporter, "cursor", _fake_cursor)

    params = QueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 2, tzinfo=UTC),
        coin="BTC",
        market_type="perpetuals",
    )

    result = exporter.export_to_csv(params, out_dir=tmp_path)

    assert result.row_count == 4
    assert result.halo_path.exists()
    assert result.aux_path.exists()

    with result.halo_path.open() as fh:
        halo_rows = list(csv.DictReader(fh))
    with result.aux_path.open() as fh:
        aux_rows = list(csv.DictReader(fh))

    assert list(halo_rows[0].keys()) == list(HALO_COLUMNS)
    assert list(aux_rows[0].keys()) == list(AUX_COLUMNS)
    assert len(halo_rows) == 4 == len(aux_rows)

    # Spot row has no PositionEffect -> written as empty, never defaulted
    # (HALO stores empty; the old NULL -> 'CLOSE' defaulting was removed 2026-08-24).
    spot = halo_rows[2]
    assert spot["PositionEffect"] == ""
    assert spot["SecurityType"] == "SPOT"
    assert spot["SymbolType"] == "Crypto"

    # SymbolType sits right after ContractMultiplier (production column order)
    # and an unmapped perp is written empty.
    keys = list(halo_rows[0].keys())
    assert keys.index("SymbolType") == keys.index("ContractMultiplier") + 1
    assert halo_rows[0]["SymbolType"] == "Crypto"
    assert halo_rows[3]["SymbolType"] == ""
    assert aux_rows[0]["_TradeId"] == "9001"

    # Id matches across halo and aux for row-level joining.
    for h, a in zip(halo_rows, aux_rows, strict=True):
        assert h["Id"] == a["Id"]

    # IsMaker is written to halo.csv as the complement of _IsTaker in aux.
    assert "IsMaker" in halo_rows[0]
    assert halo_rows[0]["IsMaker"] == "False"
    assert aux_rows[0]["_IsTaker"] == "True"

    # The fake rows are not a clean batch (tx2 and tx3 have one side only);
    # default mode reports but never blocks.
    assert not result.dq_report.ok
    assert {r.check for r in result.dq_report.failures} >= {"DQ-1", "DQ-2"}
    assert [r.check for r in result.dq_report.warnings] == ["DQ-9"]  # tx3 unmapped perp


def test_export_halo_strict_drops_is_maker_from_halo_csv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_rows: list[tuple[Any, ...]],
) -> None:
    """--halo-strict: halo.csv carries production's column set only; aux is unchanged."""
    clean_rows = [
        fake_rows[0],
        fake_rows[1],
        _make_row(Id="tx2-B", MatchingID="tx2-S", Symbol="HYPE/USDC", SecurityType="SPOT",
                  PositionEffect=None, ContractMultiplier=None, SymbolType="Crypto",
                  _SourceTradeId="tx2", _MarketType="spot", _Coin="@4", _Direction="Buy"),
        _make_row(Id="tx2-S", MatchingID="tx2-B", Side="Sell", Symbol="HYPE/USDC",
                  SecurityType="SPOT", PositionEffect=None, ContractMultiplier=None,
                  SymbolType="Crypto", _SourceTradeId="tx2", _MarketType="spot", _Coin="@4",
                  _Direction="Sell", Account="0xsell", MatchingAccount="0xbuy"),
    ]
    fake = _FakeCursor(columns=list(ALL_COLUMNS), rows=clean_rows)

    @contextmanager
    def _fake_cursor() -> Iterator[_FakeCursor]:
        yield fake

    monkeypatch.setattr(exporter, "cursor", _fake_cursor)

    params = QueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 2, tzinfo=UTC),
        halo_strict=True,
    )
    result = exporter.export_to_csv(params, out_dir=tmp_path)

    with result.halo_path.open() as fh:
        halo_rows = list(csv.DictReader(fh))
    with result.aux_path.open() as fh:
        aux_rows = list(csv.DictReader(fh))

    assert list(halo_rows[0].keys()) == list(HALO_STRICT_COLUMNS)
    assert "IsMaker" not in halo_rows[0]
    assert list(halo_rows[0].keys())[-1] == "SymbolType"
    # The maker/taker flag is still recoverable from aux.
    assert list(aux_rows[0].keys()) == list(AUX_COLUMNS)
    assert aux_rows[0]["_IsTaker"] == "True"
    assert result.row_count == 4
    assert result.dq_report.ok
    # Strict mode packages like production: one dated part (all rows share a
    # TransactTime here), production's name, aux part under aux/.
    assert result.halo_paths == (tmp_path / "sdny_LINKED_PRIVATE_EXECUTION_V2_01042024_part1.csv",)
    assert result.aux_paths == (tmp_path / "aux" / result.halo_path.name,)


def test_export_halo_strict_fails_closed_on_dq_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production ships nothing when DQ fails; a strict export removes its files."""
    rows = [_make_row(), _make_row(Id="tx1-S", MatchingID="tx1-B", Side="Sell",
                                   Account="0xsell", MatchingAccount="0xbuy"),
            _make_row(Id="tx9-B", MatchingID="tx9-S", _SourceTradeId="tx9")]  # unpaired
    fake = _FakeCursor(columns=list(ALL_COLUMNS), rows=rows)

    @contextmanager
    def _fake_cursor() -> Iterator[_FakeCursor]:
        yield fake

    monkeypatch.setattr(exporter, "cursor", _fake_cursor)
    params = QueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 2, tzinfo=UTC),
        halo_strict=True,
    )
    with pytest.raises(DqFailure) as excinfo:
        exporter.export_to_csv(params, out_dir=tmp_path)
    assert "DQ-1" in str(excinfo.value)
    assert list(tmp_path.glob("*.csv")) == []
    assert list((tmp_path / "aux").glob("*.csv")) == []


def test_export_halo_strict_clean_batch_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [_make_row(_Direction="Open Long"),
            _make_row(Id="tx1-S", MatchingID="tx1-B", Side="Sell", _Direction="Open Short",
                      Account="0xsell", MatchingAccount="0xbuy")]
    fake = _FakeCursor(columns=list(ALL_COLUMNS), rows=rows)

    @contextmanager
    def _fake_cursor() -> Iterator[_FakeCursor]:
        yield fake

    monkeypatch.setattr(exporter, "cursor", _fake_cursor)
    params = QueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 2, tzinfo=UTC),
        halo_strict=True,
    )
    result = exporter.export_to_csv(params, out_dir=tmp_path)
    assert result.dq_report.ok
    assert result.dq_report.passes == tuple(f"DQ-{i}" for i in range(1, 10))
    assert result.halo_path.exists()


def test_export_packages_parts_by_date_and_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parts roll on a new transact date and when the cap is reached; names match production."""
    day1, day2 = 1_712_000_000_000, 1_712_100_000_000  # 2024-04-01 and 2024-04-02 UTC
    rows: list[tuple[Any, ...]] = []
    for i in range(6):  # 3 trades on day 1, 3 on day 2 -> 12 rows
        ts, trade = (day1, f"a{i}") if i < 3 else (day2, f"b{i}")
        rows.append(_make_row(TransactTime=ts, Id=f"{trade}-B", MatchingID=f"{trade}-S",
                              _SourceTradeId=trade))
        rows.append(_make_row(TransactTime=ts, Id=f"{trade}-S", MatchingID=f"{trade}-B",
                              Side="Sell", _Direction="Open Short", _SourceTradeId=trade))
    fake = _FakeCursor(columns=list(ALL_COLUMNS), rows=rows)

    @contextmanager
    def _fake_cursor() -> Iterator[_FakeCursor]:
        yield fake

    monkeypatch.setattr(exporter, "cursor", _fake_cursor)
    monkeypatch.setattr(exporter._OutputWriter, "__init__",
                        _writer_init_with_frequent_size_checks)
    params = QueryParams(
        start_ts=datetime(2024, 4, 1, tzinfo=UTC),
        end_ts=datetime(2024, 4, 3, tzinfo=UTC),
        halo_strict=True,
    )
    # 0.001 MB = 1000 bytes: each ~300-byte row pair overflows every few rows.
    result = exporter.export_to_csv(params, out_dir=tmp_path, max_part_mb=0.001,
                                    file_prefix="acme")

    names = [p.name for p in result.halo_paths]
    assert names[0] == "acme_LINKED_PRIVATE_EXECUTION_V2_01042024_part1.csv"
    assert any(n.startswith("acme_LINKED_PRIVATE_EXECUTION_V2_02042024_part") for n in names)
    day1_parts = [n for n in names if "_01042024_" in n]
    day2_parts = [n for n in names if "_02042024_" in n]
    assert len(day1_parts) > 1 and len(day2_parts) > 1, names
    assert day2_parts[0].endswith("_part1.csv"), "part numbering restarts per date"
    # Every part has a header and the parts add up to all rows; aux mirrors halo.
    total = 0
    for halo_path, aux_path in zip(result.halo_paths, result.aux_paths, strict=True):
        with halo_path.open() as fh:
            part_rows = list(csv.DictReader(fh))
        with aux_path.open() as fh:
            aux_rows = list(csv.DictReader(fh))
        assert list(part_rows[0].keys()) == list(HALO_STRICT_COLUMNS)
        assert [r["Id"] for r in part_rows] == [r["Id"] for r in aux_rows]
        assert aux_path.parent == tmp_path / "aux"
        total += len(part_rows)
    assert total == 12 == result.row_count
    assert result.dq_report.ok


_ORIGINAL_WRITER_INIT = exporter._OutputWriter.__init__


def _writer_init_with_frequent_size_checks(self: Any, *args: Any, **kwargs: Any) -> None:
    """Check the part size after every row so a tiny cap splits in the test."""
    kwargs["size_check_every"] = 1
    _ORIGINAL_WRITER_INIT(self, *args, **kwargs)


def test_export_handles_uppercased_cursor_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_rows: list[tuple[Any, ...]],
) -> None:
    """Regression: Snowflake uppercases unquoted aliases.

    The driver returns column names like ``TRANSACTTIME``; the exporter
    must still populate the PascalCase HALO CSV columns. Previously the
    ``record.get(col)`` lookup missed every column, writing all-NULL rows.
    """
    upper_cols = [c.upper() for c in ALL_COLUMNS]
    fake = _FakeCursor(columns=upper_cols, rows=fake_rows)

    @contextmanager
    def _fake_cursor() -> Iterator[_FakeCursor]:
        yield fake

    monkeypatch.setattr(exporter, "cursor", _fake_cursor)

    params = QueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 2, tzinfo=UTC),
    )
    result = exporter.export_to_csv(params, out_dir=tmp_path)

    with result.halo_path.open() as fh:
        halo_rows = list(csv.DictReader(fh))

    # Previously all columns in the output would be blank. Assert the
    # critical fields carry their populated values through.
    assert halo_rows[0]["TransactTime"] == "1712000000000"
    assert halo_rows[0]["Id"] == "tx1-B"
    assert halo_rows[0]["Symbol"] == "BTC/USDC"
    assert halo_rows[0]["Side"] == "Buy"
    assert halo_rows[0]["Account"] == "0xbuy"
    assert halo_rows[0]["SecurityType"] == "SWAP"
