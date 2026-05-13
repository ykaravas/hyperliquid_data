"""Click-based CLI for the Hyperliquid -> HALO schematizer.

Two subcommands:
    * ``list-markets`` — summarise markets active in a date range.
    * ``export`` — run the mapping query and write halo.csv + aux.csv.

Typical usage::

    python -m hyperliquid_halo.cli export \\
        --start 2026-04-01 --end 2026-04-14 \\
        --coin BTC --market-type perpetuals \\
        --out-dir ./data/btc_perps_202604

    python -m hyperliquid_halo.cli list-markets \\
        --start 2026-04-01 --end 2026-04-14
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, time
from pathlib import Path

import click
from dotenv import load_dotenv

from .exporter import export_to_csv, list_markets
from .mapping import QueryParams

logger = logging.getLogger("hyperliquid_halo")


MARKET_TYPE_CHOICES = click.Choice(["spot", "perpetuals"], case_sensitive=False)


def _parse_day(value: str) -> datetime:
    """Parse a YYYY-MM-DD date string into a UTC datetime at midnight.

    Args:
        value: ISO date string (``YYYY-MM-DD``). Times are not accepted here
            to keep the CLI surface small; pass two dates for sub-day ranges
            by adding one day to ``--end``.

    Returns:
        Timezone-aware ``datetime`` at 00:00:00 UTC.

    Raises:
        click.BadParameter: If ``value`` is not a valid ISO date.
    """
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise click.BadParameter(
            f"Expected YYYY-MM-DD, got {value!r}"
        ) from exc
    return datetime.combine(parsed, time.min, tzinfo=UTC)


def _configure_logging(verbose: bool) -> None:
    """Configure logging for the CLI.

    Args:
        verbose: If True, set level to DEBUG. Otherwise INFO.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
@click.pass_context
def cli(ctx: click.Context, verbose: bool) -> None:
    """Schematize Allium Hyperliquid trades into Solidus HALO v2.1 CSVs."""
    load_dotenv()
    _configure_logging(verbose)
    ctx.ensure_object(dict)


@cli.command("export")
@click.option("--start", "start_str", required=True, metavar="YYYY-MM-DD",
              help="Inclusive start date (UTC).")
@click.option("--end", "end_str", required=True, metavar="YYYY-MM-DD",
              help="Exclusive end date (UTC). Must be > --start.")
@click.option("--coin", default=None,
              help="Filter by Allium COIN (e.g. BTC, ETH, or a spot pair id like @4).")
@click.option("--market-type", "market_type", type=MARKET_TYPE_CHOICES, default=None,
              help="Filter by market type: 'spot' or 'perpetuals'. Omit for both.")
@click.option("--token-a", default=None,
              help="Filter by TOKEN_A_SYMBOL (base token). Handy for spot markets.")
@click.option("--token-b", default=None,
              help="Filter by TOKEN_B_SYMBOL (quote token). Handy for spot markets.")
@click.option("--out-dir", type=click.Path(file_okay=False, path_type=Path),
              default=Path("./data"), show_default=True,
              help="Directory to write halo.csv and aux.csv into.")
@click.option("--halo-filename", default="halo.csv", show_default=True)
@click.option("--aux-filename", default="aux.csv", show_default=True)
def export_cmd(
    start_str: str,
    end_str: str,
    coin: str | None,
    market_type: str | None,
    token_a: str | None,
    token_b: str | None,
    out_dir: Path,
    halo_filename: str,
    aux_filename: str,
) -> None:
    """Export a HALO v2.1 CSV for the given date range and market filters."""
    params = QueryParams(
        start_ts=_parse_day(start_str),
        end_ts=_parse_day(end_str),
        coin=coin,
        market_type=market_type.lower() if market_type else None,
        token_a=token_a,
        token_b=token_b,
    )
    click.echo(
        f"Exporting {params.start_ts.date()} .. {params.end_ts.date()} "
        f"(coin={coin or 'ALL'}, market_type={market_type or 'ALL'}) -> {out_dir}"
    )
    result = export_to_csv(
        params,
        out_dir=out_dir,
        halo_filename=halo_filename,
        aux_filename=aux_filename,
    )
    click.echo(
        f"Wrote {result.row_count} rows:\n"
        f"  HALO: {result.halo_path}\n"
        f"  AUX:  {result.aux_path}"
    )


@cli.command("list-markets")
@click.option("--start", "start_str", required=True, metavar="YYYY-MM-DD")
@click.option("--end", "end_str", required=True, metavar="YYYY-MM-DD")
@click.option("--top", default=50, show_default=True,
              help="Max rows to print (markets are sorted by trade count desc).")
def list_markets_cmd(start_str: str, end_str: str, top: int) -> None:
    """List markets with trade activity in the given date range."""
    params = QueryParams(
        start_ts=_parse_day(start_str),
        end_ts=_parse_day(end_str),
    )
    rows = list_markets(params)
    if not rows:
        click.echo("No trades found in range.")
        return
    header = (
        f"{'market_type':<12} {'coin':<14} {'pair':<20} "
        f"{'token_a':<10} {'token_b':<10} {'count':>12}"
    )
    click.echo(header)
    click.echo("-" * len(header))
    for row in rows[:top]:
        click.echo(
            f"{str(row.get('MARKET_TYPE') or ''):<12} "
            f"{str(row.get('COIN') or ''):<14} "
            f"{str(row.get('PAIR') or ''):<20} "
            f"{str(row.get('TOKEN_A_SYMBOL') or ''):<10} "
            f"{str(row.get('TOKEN_B_SYMBOL') or ''):<10} "
            f"{row.get('TRADE_COUNT', 0):>12,}"
        )


def main() -> None:
    """Console-script entrypoint."""
    cli(obj={})


if __name__ == "__main__":
    sys.exit(main())
