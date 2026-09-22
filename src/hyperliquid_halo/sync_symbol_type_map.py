"""Regenerate :mod:`hyperliquid_halo.symbol_type_map` from the production map.

The production Snowflake pipeline (repo ``defi-hyperliquid-halo``) keeps the
per-market HALO ``SymbolType`` map as a ``VALUES`` list in
``db/migrations/hyperliquid/R__06b_symbol_type_map.sql``. That file is the
source of truth; this module parses it and rewrites the Python mirror so the
portable exporter emits the same ``SymbolType`` as production.

Run it whenever the production map changes (new listings, refreshed
universe workbook)::

    python -m hyperliquid_halo.sync_symbol_type_map \\
        --source ../defi-hyperliquid-halo/db/migrations/hyperliquid/R__06b_symbol_type_map.sql

The generated module is committed so the exporter has no runtime
dependency on the production repo.
"""

from __future__ import annotations

import argparse
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# One VALUES row of the production map, e.g. ``    ('xyz:TSLA', 'Equity'),``.
_ROW_RE = re.compile(r"^\s*\('([^']+)',\s*'([^']+)'\),?\s*$")

# HALO v2.1 SymbolType enum (orders and executions share one list; the
# 2026-08-24 additions FX / CryptoFlash / Announcement / Esports included).
# Kept here so a typo in the production map is caught before it lands in the
# exporter rather than at HALO upload time.
HALO_SYMBOL_TYPES: frozenset[str] = frozenset({
    "Equity", "Futures", "PerpetualFutures", "Options", "Swaps", "FixedIncome",
    "EventContracts", "Crypto", "Memecoin", "Stablecoin", "Sports", "Politics",
    "Entertainment", "Mentions", "Economics", "ScienceAndTechnology", "Elections",
    "ClimateAndWeather", "Companies", "Financials", "Social", "TokenizedEquity",
    "Commodities", "Exotics", "World", "Health", "Transportation", "FX",
    "CryptoFlash", "Announcement", "Esports",
})

_DEFAULT_OUTPUT = Path(__file__).with_name("symbol_type_map.py")


class SymbolTypeMapError(ValueError):
    """Raised when the production map cannot be parsed or fails validation."""


def parse_map_sql(sql_text: str) -> list[tuple[str, str]]:
    """Extract ``(coin, symbol_type)`` pairs from the production map SQL.

    Only lines shaped like a ``VALUES`` row are read; comments and the
    surrounding ``CREATE VIEW`` scaffolding are ignored.

    Args:
        sql_text: Full text of ``R__06b_symbol_type_map.sql``.

    Returns:
        The pairs in file order (production keeps them sorted by coin).

    Raises:
        SymbolTypeMapError: If no rows are found, a coin repeats, a value is
            not a HALO ``SymbolType``, or a coin contains a character that
            would break the generated SQL literal.

    Example:
        >>> parse_map_sql("SELECT coin, symbol_type FROM VALUES\\n"
        ...               "    ('BTC', 'Crypto'),\\n    ('xyz:TSLA', 'Equity')\\n"
        ...               "AS t(coin, symbol_type);")
        [('BTC', 'Crypto'), ('xyz:TSLA', 'Equity')]
    """
    rows: list[tuple[str, str]] = []
    for line in sql_text.splitlines():
        match = _ROW_RE.match(line)
        if match:
            rows.append((match.group(1), match.group(2)))

    if not rows:
        raise SymbolTypeMapError("no ('coin', 'symbol_type') rows found in the source SQL")

    seen: set[str] = set()
    for coin, symbol_type in rows:
        if coin in seen:
            raise SymbolTypeMapError(f"duplicate coin in production map: {coin!r}")
        seen.add(coin)
        if symbol_type not in HALO_SYMBOL_TYPES:
            raise SymbolTypeMapError(
                f"{coin!r} maps to {symbol_type!r}, which is not a HALO SymbolType"
            )
        if "'" in coin or "%" in coin or "\\" in coin:
            raise SymbolTypeMapError(f"coin {coin!r} contains a character unsafe for SQL")
    return rows


def render_module(rows: list[tuple[str, str]], source_name: str) -> str:
    """Render the Python source for :mod:`hyperliquid_halo.symbol_type_map`.

    Args:
        rows: Parsed ``(coin, symbol_type)`` pairs.
        source_name: Basename of the production SQL file, recorded in the
            generated docstring for provenance.

    Returns:
        The full module text.
    """
    generated_on = datetime.now(tz=UTC).date().isoformat()
    body = "\n".join(f"    ({coin!r}, {symbol_type!r})," for coin, symbol_type in rows)
    return f'''"""Per-market HALO ``SymbolType`` for Hyperliquid perpetual markets.

GENERATED FILE. Do not edit by hand; regenerate with::

    python -m hyperliquid_halo.sync_symbol_type_map --source <path to {source_name}>

Mirror of the production map in ``defi-hyperliquid-halo``
(``db/migrations/hyperliquid/{source_name}``), which is itself generated from
the ``hyperliquid_perps_universe.xlsx`` workbook's curated SymbolType column.
Keyed by the Allium ``COIN`` value: a bare ticker on the main dex (``BTC``)
or ``<dex>:<TICKER>`` on HIP-3 dexes (``xyz:TSLA``). Delisted markets are
included on purpose so historical trades still map on backfill.

How the exporters consume it (same policy as production):

* spot rows always emit ``Crypto`` (Hyperliquid spot is crypto-only; the map
  is not consulted);
* perp rows emit the mapped value, and nothing else: a perp missing from
  this map ships with ``SymbolType`` empty (the field is optional in HALO)
  until the map is regenerated. No guessed fallback.

Generated {generated_on} from {source_name} ({len(rows)} markets).
"""

from __future__ import annotations

SYMBOL_TYPE_MAP: tuple[tuple[str, str], ...] = (
{body}
)
"""``(coin, symbol_type)`` pairs, sorted by coin for stable diffs."""


def render_values_rows() -> str:
    """Render the map as the rows of a SQL ``VALUES`` list.

    Returns:
        One ``('coin', 'symbol_type')`` literal per line, comma separated,
        ready to splice into ``SELECT map_coin, symbol_type FROM VALUES ...``.
        Coins are validated at generation time to contain no quotes or
        ``%`` characters, so the literals need no escaping (including for
        the Snowflake connector's pyformat preprocessor).
    """
    return ",\\n        ".join(
        f"('{{coin}}', '{{symbol_type}}')" for coin, symbol_type in SYMBOL_TYPE_MAP
    )
'''


def sync(source: Path, output: Path = _DEFAULT_OUTPUT) -> int:
    """Parse the production SQL at ``source`` and rewrite ``output``.

    Args:
        source: Path to ``R__06b_symbol_type_map.sql``.
        output: Path of the generated module (defaults to the package's
            ``symbol_type_map.py``).

    Returns:
        The number of markets written.

    Raises:
        FileNotFoundError: If ``source`` does not exist.
        SymbolTypeMapError: If the source fails to parse or validate.
    """
    if not source.is_file():
        raise FileNotFoundError(f"production map not found: {source}")
    rows = parse_map_sql(source.read_text(encoding="utf-8"))
    output.write_text(render_module(rows, source.name), encoding="utf-8")
    logger.info("Wrote %d markets from %s to %s", len(rows), source, output)
    return len(rows)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: ``python -m hyperliquid_halo.sync_symbol_type_map``."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--source", type=Path, required=True,
        help="Path to the production R__06b_symbol_type_map.sql.",
    )
    parser.add_argument(
        "--output", type=Path, default=_DEFAULT_OUTPUT,
        help=f"Generated module path (default: {_DEFAULT_OUTPUT}).",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    count = sync(args.source, args.output)
    print(f"Wrote {count} markets to {args.output}")


if __name__ == "__main__":
    main()
