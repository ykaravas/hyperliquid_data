"""Unit tests for :mod:`hyperliquid_halo.orders_exporter` using a fake cursor.

No Snowflake connection is opened — the ``cursor`` context manager is
monkeypatched to yield a dummy cursor that replays canned rows.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from hyperliquid_halo import orders_exporter
from hyperliquid_halo.orders_mapping import (
    ALL_ORDER_COLUMNS,
    AUX_ORDER_COLUMNS,
    HALO_ORDER_COLUMNS,
    OrdersQueryParams,
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
    """Build a fake source row covering every column in ``ALL_ORDER_COLUMNS``."""
    defaults: dict[str, Any] = {col: None for col in ALL_ORDER_COLUMNS}
    defaults.update({
        "TransactTime": 1_712_000_000_000,
        "Id": "426914498342",
        "Symbol": "BTC/USDC",
        "Side": "Buy",
        "OrderQty": "0.5",
        "OrdType": "Limit",
        "Price": "70000",
        "Status": "New",
        "OrderCapacity": "Agency",
        "Account": "0xabc",
        "ClientId": "0xabc",
        "OriginationTrader": "0xabc",
        "LeavesQty": "0.5",
        "CumQty": "0",
        "TimeInForce": "GoodTillCancel",
        "ExVenue": "Hyperliquid",
        "TrdType": "PostOnly",
        "Blockchain": "hyperliquid",
        "WalletAddress": "0xabc",
        "SecurityType": "SWAP",
        "ExchangeSymbol": "Hyperliquid:BTC",
        "ContractMultiplier": "1",
        "_UniqueId": "status_change_timestamp-...-status-open-order_id-426914498342",
        "_RawStatus": "open",
        "_RawSide": "B",
        "_RawType": "Limit",
        "_RawTif": "Alo",
        "_Coin": "BTC",
        "_IsTrigger": False,
        "_IsTpSl": False,
        "_IsReduceOnly": False,
        "_OrderTimestamp": 1_712_000_000_000,
        "_StatusChangeTimestamp": 1_712_000_000_000,
    })
    defaults.update(overrides)
    return tuple(defaults[col] for col in ALL_ORDER_COLUMNS)


@pytest.fixture()
def fake_rows() -> list[tuple[Any, ...]]:
    return [
        # New (open) row — original placement
        _make_row(),
        # Canceled row for same order — status-change later in time
        _make_row(
            TransactTime=1_712_000_060_000,
            Status="Canceled",
            _RawStatus="canceled",
            CumQty="0",
            LeavesQty="0.5",
            OrigTransactTime=1_712_000_000_000,
            _StatusChangeTimestamp=1_712_000_060_000,
        ),
        # Spot order — @-prefixed COIN, SPOT security type, no ContractMultiplier
        _make_row(
            Id="426914498343",
            Symbol="@107/USDC",
            SecurityType="SPOT",
            ContractMultiplier=None,
            _Coin="@107",
        ),
        # Trigger order — IS_TRIGGER true, StopPx populated, TIF blank in source
        _make_row(
            Id="426914498344",
            OrdType="StopLoss",
            Status="New",
            StopPx="32232.0",
            TimeInForce="GoodTillCancel",
            TrdType="RegularTrade",
            ContingencyType="OCO",
            _IsTrigger=True,
            _IsTpSl=True,
            _RawType="Stop Market",
            _RawTif="",
            _TriggerCondition="Price above 32232",
            _TriggerPrice="32232.0",
        ),
    ]


def test_export_orders_reports_unresolved_spot_placeholders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_rows: list[tuple[Any, ...]],
) -> None:
    """A Symbol still starting with '@' means the lookback found no naming trade."""
    fake = _FakeCursor(columns=list(ALL_ORDER_COLUMNS), rows=fake_rows)

    @contextmanager
    def _fake_cursor() -> Iterator[_FakeCursor]:
        yield fake

    monkeypatch.setattr(orders_exporter, "cursor", _fake_cursor)
    params = OrdersQueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 2, tzinfo=UTC),
    )
    result = orders_exporter.export_orders_to_csv(params, out_dir=tmp_path)
    assert result.unresolved_spot_rows == 1
    assert result.unresolved_spot_coins == ("@107",)


def test_export_orders_writes_both_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_rows: list[tuple[Any, ...]],
) -> None:
    fake = _FakeCursor(columns=list(ALL_ORDER_COLUMNS), rows=fake_rows)

    @contextmanager
    def _fake_cursor() -> Iterator[_FakeCursor]:
        yield fake

    monkeypatch.setattr(orders_exporter, "cursor", _fake_cursor)

    params = OrdersQueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 2, tzinfo=UTC),
        coin="BTC",
    )
    result = orders_exporter.export_orders_to_csv(params, out_dir=tmp_path)

    assert result.row_count == 4
    assert result.halo_path.name == "halo_orders.csv"
    assert result.aux_path.name == "aux_orders.csv"
    assert result.halo_path.exists()
    assert result.aux_path.exists()

    with result.halo_path.open() as fh:
        halo_rows = list(csv.DictReader(fh))
    with result.aux_path.open() as fh:
        aux_rows = list(csv.DictReader(fh))

    assert list(halo_rows[0].keys()) == list(HALO_ORDER_COLUMNS)
    assert list(aux_rows[0].keys()) == list(AUX_ORDER_COLUMNS)
    assert len(halo_rows) == 4 == len(aux_rows)

    # Spot row carries SPOT SecurityType and a blank ContractMultiplier.
    spot = halo_rows[2]
    assert spot["SecurityType"] == "SPOT"
    assert spot["Symbol"] == "@107/USDC"
    assert spot["ContractMultiplier"] == ""

    # Trigger order carries StopPx and ContingencyType=OCO.
    trigger = halo_rows[3]
    assert trigger["StopPx"] == "32232.0"
    assert trigger["ContingencyType"] == "OCO"
    assert trigger["OrdType"] == "StopLoss"

    # Id and TransactTime form the join key between the two files.
    for h, a in zip(halo_rows, aux_rows, strict=True):
        assert h["Id"] == a["Id"]
        assert h["TransactTime"] == a["TransactTime"]


def test_export_orders_handles_uppercased_cursor_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_rows: list[tuple[Any, ...]],
) -> None:
    """Regression: Snowflake uppercases unquoted aliases."""
    upper_cols = [c.upper() for c in ALL_ORDER_COLUMNS]
    fake = _FakeCursor(columns=upper_cols, rows=fake_rows)

    @contextmanager
    def _fake_cursor() -> Iterator[_FakeCursor]:
        yield fake

    monkeypatch.setattr(orders_exporter, "cursor", _fake_cursor)

    params = OrdersQueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 2, tzinfo=UTC),
    )
    result = orders_exporter.export_orders_to_csv(params, out_dir=tmp_path)

    with result.halo_path.open() as fh:
        halo_rows = list(csv.DictReader(fh))

    # Verify critical fields carry their populated values through despite the
    # cursor returning uppercase column names.
    assert halo_rows[0]["TransactTime"] == "1712000000000"
    assert halo_rows[0]["Id"] == "426914498342"
    assert halo_rows[0]["Symbol"] == "BTC/USDC"
    assert halo_rows[0]["Side"] == "Buy"
    assert halo_rows[0]["Account"] == "0xabc"
    assert halo_rows[0]["SecurityType"] == "SWAP"
    assert halo_rows[0]["TrdType"] == "PostOnly"
