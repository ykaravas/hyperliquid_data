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
from hyperliquid_halo.mapping import ALL_COLUMNS, AUX_COLUMNS, HALO_COLUMNS, QueryParams


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
        "_SourceTradeId": "tx1",
        "_MarketType": "perpetuals",
        "_Coin": "BTC",
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
                  _MarketType="spot", _Coin="@4"),
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

    assert result.row_count == 3
    assert result.halo_path.exists()
    assert result.aux_path.exists()

    with result.halo_path.open() as fh:
        halo_rows = list(csv.DictReader(fh))
    with result.aux_path.open() as fh:
        aux_rows = list(csv.DictReader(fh))

    assert list(halo_rows[0].keys()) == list(HALO_COLUMNS)
    assert list(aux_rows[0].keys()) == list(AUX_COLUMNS)
    assert len(halo_rows) == 3 == len(aux_rows)

    # Spot row has no PositionEffect -> written as empty, never defaulted
    # (HALO stores empty; the old NULL -> 'CLOSE' defaulting was removed 2026-08-24).
    spot = halo_rows[2]
    assert spot["PositionEffect"] == ""
    assert spot["SecurityType"] == "SPOT"

    # Id matches across halo and aux for row-level joining.
    for h, a in zip(halo_rows, aux_rows, strict=True):
        assert h["Id"] == a["Id"]

    # IsMaker is written to halo.csv as the complement of _IsTaker in aux.
    assert "IsMaker" in halo_rows[0]
    assert halo_rows[0]["IsMaker"] == "False"
    assert aux_rows[0]["_IsTaker"] == "True"


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
