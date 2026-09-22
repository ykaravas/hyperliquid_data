"""Streaming data-quality checks for the executions feed.

Mirrors production's ``hyperliquid_sp_run_dq`` (``defi-hyperliquid-halo``,
``R__08_dq_proc.sql``) check for check, so a file this exporter writes is held
to the same bar as a batch production ships:

* **DQ-1** batch rows = 2 x distinct trades.
* **DQ-2** every trade has exactly one Buy and one Sell row.
* **DQ-3** required HALO fields are non-NULL.
* **DQ-4** ``SecurityType`` is ``SWAP`` or ``SPOT``.
* **DQ-5** ``Symbol`` is at most 41 characters.
* **DQ-6** ``PositionEffect`` is NULL only on SPOT rows or position flips.
* **DQ-7** every direction is in the known-eligible set (gate leak guard).
* **DQ-8** HIP-3 symbols match ``-[A-Z]+/`` and both sides agree on ``Symbol``.
* **DQ-9** (WARN) SWAP rows with an empty ``SymbolType``: the map is stale.

Production runs these as SQL over the pending batch. Here they run in one
pass over the rows as the exporter streams them, with O(1) memory: the
mapping query orders rows by ``(TransactTime, _SourceTradeId, Side)``, so
the two sides of a trade are always adjacent and the per-trade checks
(DQ-1, DQ-2, DQ-8 symmetry) only need the current group.

Severities follow production: FAIL blocks a ``--halo-strict`` export (the
files are removed, nothing is "shipped"); WARN never blocks. In the default
mode every result is reported and nothing blocks.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

REQUIRED_FIELDS: tuple[str, ...] = (
    "TransactTime", "Id", "MatchingID", "OrderID",
    "Side", "Quantity", "Price", "Symbol", "ExVenue",
)
"""HALO fields production's DQ-3 requires to be non-NULL on every row."""

FLIP_DIRS: tuple[str, ...] = ("Long > Short", "Short > Long")
"""Position flips: legitimately no open/close semantics (DQ-6)."""

ALLOWED_DIRS: tuple[str, ...] = FLIP_DIRS + (
    "Open Long", "Open Short", "Close Long", "Close Short",   # perp opens/closes
    "Settlement",                                             # exchange settlement -> CLOSE
    "Buy", "Sell",                                            # spot directions
)
"""Every direction the eligibility gate lets through. DQ-7 fails on anything
else: a gate leak or a brand-new Hyperliquid direction string. Keep in sync
with ``mapping._ELIGIBILITY_SQL`` and production's ``R__08 ALLOWED_DIRS``.
"""

MAX_SYMBOL_LEN = 41
"""HALO ``Symbol`` cap (DQ-5)."""

HIP3_SYMBOL_RE = re.compile(r".*-[A-Z]+/.*")
"""Shape of a HIP-3 symbol, ``BASE-DEX/QUOTE`` (DQ-8)."""

DQ9_TOP_SYMBOLS = 10
"""How many unmapped symbols DQ-9 names in its summary."""

_MAX_UNEXPECTED_DIRS = 50


@dataclass(frozen=True)
class DqResult:
    """Outcome of one check, in production's result shape.

    Attributes:
        check: ``'DQ-1'`` .. ``'DQ-9'``.
        description: One-line statement of what the check asserts.
        status: ``'PASS'``, ``'FAIL'`` or ``'WARN'``.
        observed: Structured evidence (counts, samples).
        summary: Human wording for WARN results; ``None`` otherwise.
    """

    check: str
    description: str
    status: str
    observed: dict[str, Any]
    summary: str | None = None


@dataclass(frozen=True)
class DqReport:
    """All check results for one export.

    Attributes:
        results: One :class:`DqResult` per check, in check order.
    """

    results: tuple[DqResult, ...]

    @property
    def failures(self) -> tuple[DqResult, ...]:
        """Results with status ``FAIL``."""
        return tuple(r for r in self.results if r.status == "FAIL")

    @property
    def warnings(self) -> tuple[DqResult, ...]:
        """Results with status ``WARN``."""
        return tuple(r for r in self.results if r.status == "WARN")

    @property
    def passes(self) -> tuple[str, ...]:
        """Check ids that passed."""
        return tuple(r.check for r in self.results if r.status == "PASS")

    @property
    def ok(self) -> bool:
        """``True`` when no check failed (warnings do not count)."""
        return not self.failures

    def describe(self) -> str:
        """One line per non-passing check, plus a pass count.

        Returns:
            Multi-line text suitable for a log message or CLI output.
        """
        lines = [f"DQ: {len(self.passes)} of {len(self.results)} checks passed"]
        for r in self.failures:
            lines.append(f"  FAIL {r.check}: {r.description}; observed {r.observed}")
        for r in self.warnings:
            lines.append(f"  WARN {r.check}: {r.summary}")
        return "\n".join(lines)


class DqFailure(RuntimeError):
    """Raised by a ``--halo-strict`` export whose rows fail a blocking check.

    Attributes:
        report: The full :class:`DqReport`.
    """

    def __init__(self, report: DqReport) -> None:
        failing = ", ".join(r.check for r in report.failures)
        super().__init__(f"DQ FAIL ({failing}); nothing written")
        self.report = report


def _is_null(value: Any) -> bool:
    """Treat ``None`` and the empty string as NULL (the CSV writes both as empty)."""
    return value is None or value == ""


@dataclass
class _TradeGroup:
    """Rolling state for the trade whose rows are currently streaming."""

    key: str
    sides: list[str] = field(default_factory=list)
    symbols: set[str] = field(default_factory=set)
    security_type: str | None = None
    symbol_type_null: bool = False
    symbol: str | None = None


class ExecutionDqChecker:
    """Accumulates the nine checks over rows streamed in query order.

    Feed every row through :meth:`observe` (HALO row plus its aux row), then
    call :meth:`finish` once to close the last trade group and build the
    :class:`DqReport`. Rows must arrive ordered by
    ``(TransactTime, _SourceTradeId, Side)``, which is how
    :func:`hyperliquid_halo.mapping.build_query` orders them.

    Example:
        >>> checker = ExecutionDqChecker()
        >>> for halo_row, aux_row in rows:  # doctest: +SKIP
        ...     checker.observe(halo_row, aux_row)
        >>> report = checker.finish()  # doctest: +SKIP
        >>> report.ok  # doctest: +SKIP
        True
    """

    def __init__(self) -> None:
        self._rows = 0
        self._trades = 0
        self._dq2_violations = 0
        self._null_counts: dict[str, int] = dict.fromkeys(REQUIRED_FIELDS, 0)
        self._dq4_violations = 0
        self._dq5_violations = 0
        self._dq5_max_len = 0
        self._dq6_violations = 0
        self._dq7_violations = 0
        self._dq7_unexpected: set[str] = set()
        self._dq8_pattern_violations = 0
        self._dq8_symmetry_violations = 0
        self._dq9_rows: dict[str, int] = {}
        self._dq9_trades: dict[str, int] = {}
        self._group: _TradeGroup | None = None
        self._finished = False

    def observe(self, halo_row: Mapping[str, Any], aux_row: Mapping[str, Any]) -> None:
        """Account for one execution row.

        Args:
            halo_row: The HALO-column values of the row (any column set that
                includes the fields the checks read).
            aux_row: The aux-column values (``_SourceTradeId``, ``_Direction``,
                ``_IsHip3``).

        Raises:
            RuntimeError: If called after :meth:`finish`.
        """
        if self._finished:
            raise RuntimeError("ExecutionDqChecker.observe() called after finish()")
        self._rows += 1

        key = str(aux_row.get("_SourceTradeId"))
        if self._group is None or self._group.key != key:
            self._close_group()
            self._group = _TradeGroup(key=key)
        group = self._group

        side = halo_row.get("Side")
        symbol = halo_row.get("Symbol")
        security_type = halo_row.get("SecurityType")
        direction = aux_row.get("_Direction")

        group.sides.append(str(side))
        group.symbols.add("" if _is_null(symbol) else str(symbol))
        group.security_type = security_type
        group.symbol = None if _is_null(symbol) else str(symbol)
        if security_type == "SWAP" and _is_null(halo_row.get("SymbolType")):
            group.symbol_type_null = True

        # DQ-3: required fields non-NULL.
        for column in REQUIRED_FIELDS:
            if _is_null(halo_row.get(column)):
                self._null_counts[column] += 1

        # DQ-4: SecurityType enum.
        if security_type not in ("SWAP", "SPOT"):
            self._dq4_violations += 1

        # DQ-5: Symbol length.
        if not _is_null(symbol):
            length = len(str(symbol))
            self._dq5_max_len = max(self._dq5_max_len, length)
            if length > MAX_SYMBOL_LEN:
                self._dq5_violations += 1

        # DQ-6: PositionEffect NULL only on SPOT or flips.
        if (
            security_type == "SWAP"
            and _is_null(halo_row.get("PositionEffect"))
            and direction not in FLIP_DIRS
        ):
            self._dq6_violations += 1

        # DQ-7: direction whitelist (NULL fails too).
        if direction is None or direction not in ALLOWED_DIRS:
            self._dq7_violations += 1
            if len(self._dq7_unexpected) < _MAX_UNEXPECTED_DIRS:
                self._dq7_unexpected.add(str(direction))

        # DQ-8 (pattern half): HIP-3 symbols look like BASE-DEX/QUOTE.
        is_hip3 = aux_row.get("_IsHip3")
        if is_hip3 is True and (_is_null(symbol) or not HIP3_SYMBOL_RE.match(str(symbol))):
            self._dq8_pattern_violations += 1

    def _close_group(self) -> None:
        """Finalize the per-trade checks for the group that just ended."""
        group = self._group
        if group is None:
            return
        self._trades += 1
        # DQ-2: exactly one Buy and one Sell.
        if group.sides.count("Buy") != 1 or group.sides.count("Sell") != 1:
            self._dq2_violations += 1
        # DQ-8 (symmetry half): both sides agree on Symbol.
        if len(group.symbols) > 1:
            self._dq8_symmetry_violations += 1
        # DQ-9: unmapped perp symbol, counted per symbol.
        if group.symbol_type_null and group.security_type == "SWAP":
            symbol = group.symbol or ""
            self._dq9_rows[symbol] = self._dq9_rows.get(symbol, 0) + len(group.sides)
            self._dq9_trades[symbol] = self._dq9_trades.get(symbol, 0) + 1

    def finish(self) -> DqReport:
        """Close the last trade group and evaluate every check.

        Returns:
            The :class:`DqReport` with one result per check.
        """
        if not self._finished:
            self._close_group()
            self._group = None
            self._finished = True

        def verdict(ok: bool) -> str:
            return "PASS" if ok else "FAIL"

        results: list[DqResult] = []

        expected = 2 * self._trades
        results.append(DqResult(
            "DQ-1", "batch rows = 2 x distinct trades", verdict(self._rows == expected),
            {"rows": self._rows, "trades": self._trades, "expected": expected},
        ))
        results.append(DqResult(
            "DQ-2", "each _SourceTradeId has exactly one Buy and one Sell row",
            verdict(self._dq2_violations == 0),
            {"violating_source_trades": self._dq2_violations},
        ))
        total_nulls = sum(self._null_counts.values())
        results.append(DqResult(
            "DQ-3", f"required fields are non-NULL ({', '.join(REQUIRED_FIELDS)})",
            verdict(total_nulls == 0),
            {"null_counts": dict(self._null_counts), "total_nulls": total_nulls},
        ))
        results.append(DqResult(
            "DQ-4", "SecurityType in (SWAP, SPOT)", verdict(self._dq4_violations == 0),
            {"violations": self._dq4_violations},
        ))
        results.append(DqResult(
            "DQ-5", f"LENGTH(Symbol) <= {MAX_SYMBOL_LEN}", verdict(self._dq5_violations == 0),
            {"violations": self._dq5_violations, "max_observed_length": self._dq5_max_len},
        ))
        results.append(DqResult(
            "DQ-6", "PositionEffect NULL only for SPOT or flip-direction rows",
            verdict(self._dq6_violations == 0),
            {"violating_derivatives_rows": self._dq6_violations},
        ))
        results.append(DqResult(
            "DQ-7", "all _Direction values in the known-eligible direction set",
            verdict(self._dq7_violations == 0),
            {"violations": self._dq7_violations,
             "unexpected_dirs": sorted(self._dq7_unexpected)},
        ))
        results.append(DqResult(
            "DQ-8", "HIP-3 Symbols match -[A-Z]+/ pattern and both sides agree",
            verdict(self._dq8_pattern_violations == 0 and self._dq8_symmetry_violations == 0),
            {"pattern_violations": self._dq8_pattern_violations,
             "symmetry_violations": self._dq8_symmetry_violations},
        ))
        results.append(self._dq9())
        return DqReport(results=tuple(results))

    def _dq9(self) -> DqResult:
        """WARN-level: SWAP rows shipping with an empty SymbolType (stale map)."""
        rows = sum(self._dq9_rows.values())
        trades = sum(self._dq9_trades.values())
        symbols = len(self._dq9_rows)
        top = sorted(self._dq9_rows.items(), key=lambda kv: (-kv[1], kv[0]))[:DQ9_TOP_SYMBOLS]
        observed = {
            "rows": rows, "trades": trades, "symbols": symbols,
            "top_symbols": [{"symbol": s, "rows": n} for s, n in top],
        }
        description = "no SWAP rows with empty SymbolType (symbol-type map up to date)"
        if rows == 0:
            return DqResult("DQ-9", description, "PASS", observed)
        more = symbols - len(top)
        summary = (
            f"SymbolType empty on {rows} SWAP rows ({trades} trades, {symbols} symbols): "
            + ", ".join(f"{s} {n}" for s, n in top)
            + (f", +{more} more" if more > 0 else "")
            + ". Regenerate symbol_type_map.py from production's R__06b "
            "(python -m hyperliquid_halo.sync_symbol_type_map)."
        )
        return DqResult("DQ-9", description, "WARN", observed, summary)
