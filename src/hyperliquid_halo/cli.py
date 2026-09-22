"""Click-based CLI for the Hyperliquid -> HALO schematizer.

Subcommands:
    * ``list-markets`` — summarise trade markets active in a date range.
    * ``export-execs`` — run the trades mapping query and write
      halo.csv + aux.csv.
    * ``list-order-coins`` — summarise order activity by COIN.
    * ``export-orders`` — run the orders mapping query and write
      halo_orders.csv + aux_orders.csv.

Typical usage::

    python -m hyperliquid_halo.cli export-execs \\
        --start 2026-04-01 --end 2026-04-14 \\
        --coin BTC --market-type perpetuals \\
        --out-dir ./output/btc_perps_202604

    python -m hyperliquid_halo.cli export-orders \\
        --start 2026-04-01 --end 2026-04-14 \\
        --coin BTC --market-type perpetuals \\
        --out-dir ./output/btc_perps_202604

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

from .dq import DqFailure
from .exporter import export_to_csv, list_markets
from .mapping import QueryParams
from .orders_exporter import export_orders_to_csv, list_order_coins
from .orders_mapping import OrdersQueryParams

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


@cli.command("export-execs")
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
              default=Path("./output"), show_default=True,
              help="Directory to write halo.csv and aux.csv into.")
@click.option("--halo-filename", default="halo.csv", show_default=True)
@click.option("--aux-filename", default="aux.csv", show_default=True)
@click.option("--include-ineligible", is_flag=True, default=False,
              help="Skip the production eligibility gate: also export liquidations, "
                   "auto-deleveraging, vault aggregation rows, dust sweeps and trades "
                   "with unresolved symbols (research use; not what production ships).")
@click.option("--halo-strict", is_flag=True, default=False,
              help="Emit exactly what production ships to HALO: OrderID/MatchingOrderID "
                   "carry the -B/-S side suffix, no IsMaker column, DQ failures remove the "
                   "files, and output is packaged as per-date parts (see --max-file-mb). "
                   "Cannot be combined with --include-ineligible.")
@click.option("--max-file-mb", type=click.FloatRange(min=1), default=None,
              help="Package the HALO output as per-transact-date part files capped at this "
                   "many MB, named like production's "
                   "({prefix}_LINKED_PRIVATE_EXECUTION_V2_DDMMYYYY_partN.csv; aux parts "
                   "under aux/). Default: 250 with --halo-strict (production's cap), "
                   "otherwise single files.")
@click.option("--file-prefix", default="sdny", show_default=True,
              help="File-name prefix for packaged parts (production uses the tenant name).")
def export_execs_cmd(
    start_str: str,
    end_str: str,
    coin: str | None,
    market_type: str | None,
    token_a: str | None,
    token_b: str | None,
    out_dir: Path,
    halo_filename: str,
    aux_filename: str,
    include_ineligible: bool,
    halo_strict: bool,
    max_file_mb: float | None,
    file_prefix: str,
) -> None:
    """Export a HALO v2.1 Execution Data CSV for the given date range and market filters."""
    if halo_strict and include_ineligible:
        raise click.UsageError("--halo-strict cannot be combined with --include-ineligible.")
    params = QueryParams(
        start_ts=_parse_day(start_str),
        end_ts=_parse_day(end_str),
        coin=coin,
        market_type=market_type.lower() if market_type else None,
        token_a=token_a,
        token_b=token_b,
        include_ineligible=include_ineligible,
        halo_strict=halo_strict,
    )
    click.echo(
        f"Exporting {params.start_ts.date()} .. {params.end_ts.date()} "
        f"(coin={coin or 'ALL'}, market_type={market_type or 'ALL'}, "
        f"eligibility={'off' if include_ineligible else 'production'}, "
        f"mode={'halo-strict' if halo_strict else 'default'}) -> {out_dir}"
    )
    try:
        result = export_to_csv(
            params,
            out_dir=out_dir,
            halo_filename=halo_filename,
            aux_filename=aux_filename,
            max_part_mb=max_file_mb,
            file_prefix=file_prefix,
        )
    except DqFailure as exc:
        # Production aborts the upload on a DQ failure; a strict export does
        # the same and leaves no files behind.
        click.echo(f"{exc}\n{exc.report.describe()}", err=True)
        raise SystemExit(1) from exc
    if len(result.halo_paths) == 1:
        click.echo(
            f"Wrote {result.row_count} rows:\n"
            f"  HALO: {result.halo_path}\n"
            f"  AUX:  {result.aux_path}"
        )
    else:
        click.echo(
            f"Wrote {result.row_count} rows as {len(result.halo_paths)} HALO part file(s) "
            f"under {out_dir} (aux parts under {out_dir / 'aux'}):"
        )
        for path in result.halo_paths:
            click.echo(f"  {path.name}  ({path.stat().st_size / 1e6:.1f} MB)")
    click.echo(result.dq_report.describe())


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


@cli.command("export-orders")
@click.option("--start", "start_str", required=True, metavar="YYYY-MM-DD",
              help="Inclusive start date (UTC, on ORDER_TIMESTAMP).")
@click.option("--end", "end_str", required=True, metavar="YYYY-MM-DD",
              help="Exclusive end date (UTC). Must be > --start.")
@click.option("--coin", default=None,
              help="Filter by Allium COIN (e.g. BTC, kPEPE, xyz:SP500, @107, PURR/USDC).")
@click.option("--market-type", "market_type", type=MARKET_TYPE_CHOICES, default=None,
              help="Filter by inferred market type (from COIN shape): "
                   "'spot' (@N or base/quote) or 'perpetuals' (everything else). "
                   "Omit for both.")
@click.option("--user", "user_addr", default=None,
              help="Filter by on-chain user address (the RAW.ORDERS USER column). "
                   "Useful for per-account analysis.")
@click.option("--out-dir", type=click.Path(file_okay=False, path_type=Path),
              default=Path("./output"), show_default=True,
              help="Directory to write halo_orders.csv and aux_orders.csv into.")
@click.option("--halo-filename", default="halo_orders.csv", show_default=True)
@click.option("--aux-filename", default="aux_orders.csv", show_default=True)
@click.option("--spot-lookback-days", type=click.IntRange(min=0), default=30, show_default=True,
              help="Days before --start to scan DEX.TRADES when resolving @N spot pair ids "
                   "to token symbols (e.g. @107 -> HYPE/USDC). Pairs with no trade in the "
                   "window keep an @N/USDC placeholder and are listed in a warning.")
def export_orders_cmd(
    start_str: str,
    end_str: str,
    coin: str | None,
    market_type: str | None,
    user_addr: str | None,
    out_dir: Path,
    halo_filename: str,
    aux_filename: str,
    spot_lookback_days: int,
) -> None:
    """Export a HALO v2.1 Order Data CSV for the given date range and filters.

    Implements the mapping in ``docs/hl_orders_halo_mapping.md``. ``filled``
    rows are filtered out (covered by the trades pipeline) and ``Vault Close``
    orders are dropped (out of scope today).
    """
    params = OrdersQueryParams(
        start_ts=_parse_day(start_str),
        end_ts=_parse_day(end_str),
        coin=coin,
        market_type=market_type.lower() if market_type else None,
        user=user_addr,
        spot_lookback_days=spot_lookback_days,
    )
    click.echo(
        f"Exporting orders {params.start_ts.date()} .. {params.end_ts.date()} "
        f"(coin={coin or 'ALL'}, market_type={market_type or 'ALL'}, "
        f"user={user_addr or 'ALL'}) -> {out_dir}"
    )
    result = export_orders_to_csv(
        params,
        out_dir=out_dir,
        halo_filename=halo_filename,
        aux_filename=aux_filename,
    )
    click.echo(
        f"Wrote {result.row_count} order rows:\n"
        f"  HALO: {result.halo_path}\n"
        f"  AUX:  {result.aux_path}"
    )
    if result.unresolved_spot_rows:
        click.echo(
            f"WARNING: {result.unresolved_spot_rows} rows on "
            f"{len(result.unresolved_spot_coins)} spot pair(s) kept the @N/USDC placeholder "
            f"Symbol (no trade in the {spot_lookback_days}-day lookback): "
            f"{', '.join(result.unresolved_spot_coins)}",
            err=True,
        )


@cli.command("list-order-coins")
@click.option("--start", "start_str", required=True, metavar="YYYY-MM-DD")
@click.option("--end", "end_str", required=True, metavar="YYYY-MM-DD")
@click.option("--top", default=50, show_default=True,
              help="Max rows to print (sorted by event count desc).")
def list_order_coins_cmd(start_str: str, end_str: str, top: int) -> None:
    """List distinct COIN values with order activity in the given date range."""
    params = OrdersQueryParams(
        start_ts=_parse_day(start_str),
        end_ts=_parse_day(end_str),
    )
    rows = list_order_coins(params)
    if not rows:
        click.echo("No orders found in range.")
        return
    header = (
        f"{'coin':<22} {'market_type':<12} {'events':>14} "
        f"{'unique_orders':>14} {'unique_users':>14}"
    )
    click.echo(header)
    click.echo("-" * len(header))
    for row in rows[:top]:
        click.echo(
            f"{str(row.get('COIN') or ''):<22} "
            f"{str(row.get('INFERRED_MARKET_TYPE') or ''):<12} "
            f"{row.get('EVENT_COUNT', 0):>14,} "
            f"{row.get('UNIQUE_ORDERS', 0):>14,} "
            f"{row.get('UNIQUE_USERS', 0):>14,}"
        )


def main() -> None:
    """Console-script entrypoint."""
    cli(obj={})


if __name__ == "__main__":
    sys.exit(main())
