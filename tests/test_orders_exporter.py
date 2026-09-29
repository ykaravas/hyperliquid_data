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
        "Id": "426914498342-B",
        "Symbol": "BTC/USDC",
        "Side": "Buy",
        "OrderQty": "0.5",
        "OrdType": "Limit",
        "Price": "70000",
        "Notional": "35000",
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
        "Text": "open",
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
            Text="canceled",
            _RawStatus="canceled",
            CumQty="0",
            LeavesQty="0.5",
            OrigTransactTime=1_712_000_000_000,
            _StatusChangeTimestamp=1_712_000_060_000,
        ),
        # Spot order — @-prefixed COIN, SPOT security type, no ContractMultiplier
        _make_row(
            Id="426914498343-B",
            Symbol="@107/USDC",
            SecurityType="SPOT",
            ContractMultiplier=None,
            _Coin="@107",
        ),
        # Trigger order — IS_TRIGGER true, StopPx populated, TIF blank in source
        _make_row(
            Id="426914498344-B",
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


def _zero_qty_stop_row() -> tuple[Any, ...]:
    """A full-position stop: Allium reports ORIGINAL_SIZE 0 until it triggers."""
    return _make_row(
        Id="426914498345-B",
        OrderQty="0",
        LeavesQty="0",
        OrdType="StopLoss",
        StopPx="31000.0",
        TimeInForce="GoodTillCancel",
        TrdType="RegularTrade",
        _IsTrigger=True,
        _IsTpSl=True,
        _RawType="Stop Market",
    )


@pytest.mark.parametrize("drop", [False, True])
def test_export_orders_counts_and_optionally_withholds_zero_qty_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_rows: list[tuple[Any, ...]],
    drop: bool,
) -> None:
    """OrderQty 0 rows are always counted and kept in aux; withheld from HALO by default (§5.8)."""
    fake = _FakeCursor(columns=list(ALL_ORDER_COLUMNS), rows=fake_rows + [_zero_qty_stop_row()])

    @contextmanager
    def _fake_cursor() -> Iterator[_FakeCursor]:
        yield fake

    monkeypatch.setattr(orders_exporter, "cursor", _fake_cursor)
    params = OrdersQueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 2, tzinfo=UTC),
    )
    result = orders_exporter.export_orders_to_csv(params, out_dir=tmp_path, drop_zero_qty=drop)

    with result.halo_path.open() as fh:
        halo_ids = [r["Id"] for r in csv.DictReader(fh)]
    with result.aux_path.open() as fh:
        aux_ids = [r["Id"] for r in csv.DictReader(fh)]

    assert result.zero_qty_rows == 1
    assert result.zero_qty_dropped is drop
    assert result.sized_from_position_rows == 0
    assert "426914498345-B" in aux_ids  # aux always keeps the row
    if drop:
        assert "426914498345-B" not in halo_ids
        assert result.row_count == len(fake_rows) == len(halo_ids)
    else:
        assert "426914498345-B" in halo_ids
        assert result.row_count == len(fake_rows) + 1 == len(halo_ids)


def test_export_orders_halo_strict_writes_parts_and_one_aux_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_rows: list[tuple[Any, ...]],
) -> None:
    """Strict mode: size-capped PRIVATE_ORDER_V2 parts named for the export day, aux once."""
    fake = _FakeCursor(columns=list(ALL_ORDER_COLUMNS), rows=fake_rows)

    @contextmanager
    def _fake_cursor() -> Iterator[_FakeCursor]:
        yield fake

    monkeypatch.setattr(orders_exporter, "cursor", _fake_cursor)
    params = OrdersQueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 2, tzinfo=UTC),
    )
    # A 200-byte cap checked on every row: each fake row is longer, so one row per part.
    result = orders_exporter.export_orders_to_csv(
        params, out_dir=tmp_path, halo_strict=True, max_part_mb=0.0002, size_check_every=1
    )
    names = [p.name for p in result.halo_paths]
    expected = [f"sdny_PRIVATE_ORDER_V2_01042026_part{i}.csv" for i in range(1, len(fake_rows) + 1)]
    assert names == expected
    assert result.halo_path == result.halo_paths[0]
    assert not (tmp_path / "halo_orders.csv").exists()
    assert not (tmp_path / "aux").exists()  # aux is one file, never parts
    part_rows = []
    for p in result.halo_paths:
        with p.open() as fh:
            rows = list(csv.DictReader(fh))
            assert list(rows[0].keys()) == list(HALO_ORDER_COLUMNS)
            part_rows.extend(rows)
    with result.aux_path.open() as fh:
        aux_rows = list(csv.DictReader(fh))
    assert len(part_rows) == len(aux_rows) == len(fake_rows) == result.row_count
    assert [r["Id"] for r in part_rows] == [r["Id"] for r in aux_rows]


def test_export_orders_halo_strict_requires_a_one_day_window(tmp_path: Path) -> None:
    params = OrdersQueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 3, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="one day per run"):
        orders_exporter.export_orders_to_csv(params, out_dir=tmp_path, halo_strict=True)


def test_export_orders_counts_rows_sized_from_the_position(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_rows: list[tuple[Any, ...]],
) -> None:
    """A full-position stop sized by the SQL carries _PositionSize and a real OrderQty."""
    sized = _make_row(
        Id="426914498346-S", Side="Sell", OrderQty="1.25", LeavesQty="1.25", OrdType="StopLoss",
        StopPx="31000.0", _IsTrigger=True, _IsTpSl=True, _RawType="Stop Market",
        _PositionSize="1.25",
    )
    fake = _FakeCursor(columns=list(ALL_ORDER_COLUMNS), rows=fake_rows + [sized])

    @contextmanager
    def _fake_cursor() -> Iterator[_FakeCursor]:
        yield fake

    monkeypatch.setattr(orders_exporter, "cursor", _fake_cursor)
    params = OrdersQueryParams(
        start_ts=datetime(2026, 4, 1, tzinfo=UTC), end_ts=datetime(2026, 4, 2, tzinfo=UTC)
    )
    result = orders_exporter.export_orders_to_csv(params, out_dir=tmp_path)  # default: drop zeros
    assert result.sized_from_position_rows == 1
    assert result.zero_qty_rows == 0
    assert result.zero_qty_dropped is True
    assert result.row_count == len(fake_rows) + 1


def test_is_zero_quantity_handles_strings_numbers_and_junk() -> None:
    assert orders_exporter._is_zero_quantity("0")
    assert orders_exporter._is_zero_quantity("0.000")
    assert orders_exporter._is_zero_quantity(0)
    assert not orders_exporter._is_zero_quantity("0.5")
    assert not orders_exporter._is_zero_quantity(None)
    assert not orders_exporter._is_zero_quantity("abc")


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

    # Text carries the raw HL status, the same value aux keeps in _RawStatus.
    assert halo_rows[1]["Text"] == "canceled" == aux_rows[1]["_RawStatus"]

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
    assert halo_rows[0]["Id"] == "426914498342-B"
    assert halo_rows[0]["Symbol"] == "BTC/USDC"
    assert halo_rows[0]["Side"] == "Buy"
    assert halo_rows[0]["Account"] == "0xabc"
    assert halo_rows[0]["SecurityType"] == "SWAP"
    assert halo_rows[0]["TrdType"] == "PostOnly"
