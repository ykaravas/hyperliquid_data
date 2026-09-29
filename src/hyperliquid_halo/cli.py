"""Click-based CLI for the Hyperliquid -> HALO schematizer.

Subcommands:
    * ``list-markets`` — summarise trade markets active in a date range.
    * ``export-execs`` — run the trades mapping query and write
      halo.csv + aux.csv.
    * ``list-order-coins`` — summarise order activity by COIN.
    * ``export-orders`` — run the orders mapping query and write
      halo_orders.csv + aux_orders.csv.
    * ``check-coverage``: what a HALO tenant already holds per day.
    * ``export-window``: per-day HALO-strict export of a window, gated on
      the tenant coverage check, with a resumable ledger.
    * ``upload-window``: resume-safe upload of an exported window through
      the halo-upload skill.

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
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from pathlib import Path

import click
from dotenv import load_dotenv

from . import coverage, upload, window
from .dq import DqFailure
from .exporter import DEFAULT_FILE_PREFIX, export_to_csv, list_markets
from .mapping import QueryParams
from .orders_exporter import DEFAULT_ORDERS_PART_MB, export_orders_to_csv, list_order_coins
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
@click.option("--trigger-lookback-days", type=click.IntRange(min=0), default=14, show_default=True,
              help="Days before --start to scan RAW.ORDERS for the armed rows of trigger "
                   "orders. A fired trigger order is re-stamped to its trigger time, so its "
                   "post-trigger rows need the earlier armed rows for StopPx, the original "
                   "placement time and OCO pairing; an order armed before the lookback is "
                   "reported as a plain Market / Limit order.")
@click.option("--halo-strict", is_flag=True, default=False,
              help="Package the HALO rows as upload-ready parts named "
                   "{prefix}_PRIVATE_ORDER_V2_DDMMYYYY_partN.csv, capped at --max-file-mb "
                   "each (one day per run). aux_orders.csv is still one file.")
@click.option("--max-file-mb", type=click.FloatRange(min=1), default=DEFAULT_ORDERS_PART_MB,
              show_default=True, help="Per-part size cap in MB under --halo-strict.")
@click.option("--file-prefix", default=DEFAULT_FILE_PREFIX, show_default=True,
              help="Prefix of the part names under --halo-strict.")
@click.option("--exclude-post-only", is_flag=True, default=False,
              help="Drop post-only (TIME_IN_FORCE = Alo) rows, the market-maker quoting "
                   "traffic that is about 98% of a day's order events (1.5 billion rows on "
                   "2026-03-02; 31 million without them). Gtc, Ioc, market and all trigger "
                   "orders are kept.")
@click.option("--position-lookback-days", type=click.IntRange(min=0), default=30,
              show_default=True,
              help="Days before --start to scan DEX.TRADES for the latest fill that gives a "
                   "trader's position, used as the size of full-position TP/SL orders "
                   "(ORIGINAL_SIZE 0).")
@click.option("--drop-zero-qty/--keep-zero-qty", default=True, show_default=True,
              help="Withhold rows whose OrderQty is still 0 after the position lookup from "
                   "the HALO file (HALO rejects a zero quantity). They stay in aux_orders.csv "
                   "and are counted either way.")
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
    trigger_lookback_days: int,
    halo_strict: bool,
    max_file_mb: float,
    file_prefix: str,
    exclude_post_only: bool,
    position_lookback_days: int,
    drop_zero_qty: bool,
) -> None:
    """Export a HALO v2.1 Order Data CSV for the given date range and filters.

    Implements the mapping in ``docs/hl_orders_halo_mapping.md``. ``filled``
    rows are filtered out (covered by the trades pipeline), ``Vault Close``
    orders and ``#N`` outcome-market rows are dropped (out of scope), and
    full-position TP/SL rows are sized from the trader's position at
    placement; the few still at ``OrderQty`` 0 are withheld from the HALO
    file unless ``--keep-zero-qty``.
    """
    params = OrdersQueryParams(
        start_ts=_parse_day(start_str),
        end_ts=_parse_day(end_str),
        coin=coin,
        market_type=market_type.lower() if market_type else None,
        user=user_addr,
        spot_lookback_days=spot_lookback_days,
        trigger_lookback_days=trigger_lookback_days,
        position_lookback_days=position_lookback_days,
        exclude_post_only=exclude_post_only,
    )
    click.echo(
        f"Exporting orders {params.start_ts.date()} .. {params.end_ts.date()} "
        f"(coin={coin or 'ALL'}, market_type={market_type or 'ALL'}, "
        f"user={user_addr or 'ALL'}, post_only={'excluded' if exclude_post_only else 'kept'}"
        f"{', HALO-strict parts' if halo_strict else ''}) -> {out_dir}"
    )
    try:
        result = export_orders_to_csv(
            params,
            out_dir=out_dir,
            halo_filename=halo_filename,
            aux_filename=aux_filename,
            drop_zero_qty=drop_zero_qty,
            halo_strict=halo_strict,
            max_part_mb=max_file_mb,
            file_prefix=file_prefix,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    halo_desc = (
        f"{len(result.halo_paths)} part(s), first {result.halo_path}"
        if halo_strict else str(result.halo_path)
    )
    click.echo(
        f"Wrote {result.row_count} order rows:\n"
        f"  HALO: {halo_desc}\n"
        f"  AUX:  {result.aux_path}"
    )
    if result.sized_from_position_rows:
        click.echo(
            f"NOTE: {result.sized_from_position_rows} full-position TP/SL rows were sized from "
            "the trader's position at placement (aux _PositionSize).",
            err=True,
        )
    if result.zero_qty_rows:
        action = ("withheld from the HALO file, kept in aux" if result.zero_qty_dropped
                  else "kept in the HALO file with OrderQty 0, which HALO rejects")
        click.echo(
            f"NOTE: {result.zero_qty_rows} full-position TP/SL rows still have OrderQty 0 "
            f"(no resolvable position): {action}.",
            err=True,
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


def _parse_date(value: str) -> date:
    """Parse ``YYYY-MM-DD`` into a :class:`date` for the window commands."""
    return _parse_day(value).date()


def _window_dates(start_str: str, end_str: str) -> tuple[date, date]:
    """Validate and return ``(start, end_exclusive)`` for the window commands.

    Raises:
        click.UsageError: If ``--end`` is not after ``--start``.
    """
    start, end_exclusive = _parse_date(start_str), _parse_date(end_str)
    if end_exclusive <= start:
        raise click.UsageError("--end must be after --start (it is exclusive).")
    return start, end_exclusive


def _clickhouse_options(command: Callable[..., None]) -> Callable[..., None]:
    """Attach the ClickHouse (clickhouse-download skill) options to a command."""
    options = [
        click.option("--ch-env", default=coverage.DEFAULT_CH_ENV, show_default=True,
                     help="Named environment in the clickhouse-download skill's .env."),
        click.option("--ch-database", default=coverage.DEFAULT_CH_DATABASE, show_default=True,
                     help="ClickHouse database holding strict_events."),
        click.option("--ch-script", type=click.Path(path_type=Path), default=None,
                     help=f"Path of the clickhouse-download skill script "
                          f"(default: ${coverage.SCRIPT_ENV} or {coverage.DEFAULT_SCRIPT})."),
        click.option("--ch-python", default=None,
                     help=f"Interpreter with clickhouse-connect that runs the skill "
                          f"(default: ${coverage.PYTHON_ENV} or {coverage.DEFAULT_PYTHON!r})."),
    ]
    for option in reversed(options):
        command = option(command)
    return command


def _coverage_report(
    tenant: str,
    start: date,
    end_exclusive: date,
    ch_env: str,
    ch_database: str,
    ch_script: Path | None,
    ch_python: str | None,
) -> coverage.CoverageReport:
    """Run the tenant coverage query and print its table.

    Raises:
        click.ClickException: If the skill is missing or the query fails.
    """
    try:
        settings = coverage.resolve_settings(ch_script, ch_python, ch_env, ch_database)
        click.echo(f"Checking what {tenant} already holds for {start} .. {end_exclusive} "
                   f"(ClickHouse {ch_env}/{ch_database})...")
        report = coverage.check_coverage(tenant, start, end_exclusive, settings)
    except coverage.CoverageError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(coverage.format_report(report))
    return report


@cli.command("check-coverage")
@click.option("--start", "start_str", required=True, metavar="YYYY-MM-DD",
              help="Inclusive start transact date (UTC).")
@click.option("--end", "end_str", required=True, metavar="YYYY-MM-DD",
              help="Exclusive end transact date (UTC). Must be > --start.")
@click.option("--tenant", required=True, help="HALO tenant, e.g. HLRESEARCH.")
@_clickhouse_options
def check_coverage_cmd(
    start_str: str,
    end_str: str,
    tenant: str,
    ch_env: str,
    ch_database: str,
    ch_script: Path | None,
    ch_python: str | None,
) -> None:
    """Show what a HALO tenant already holds per day and per source file.

    Exits 2 when any day in the range already has rows, so the command can
    gate a shell pipeline; `export-window` runs the same check itself.
    """
    start, end_exclusive = _window_dates(start_str, end_str)
    report = _coverage_report(tenant, start, end_exclusive, ch_env, ch_database, ch_script,
                              ch_python)
    if report.covered_days:
        click.echo(f"{len(report.covered_days)} day(s) already on {tenant}; do not export them "
                   "again without a purge.", err=True)
        raise SystemExit(2)
    click.echo(f"Nothing on {tenant} for {start} .. {end_exclusive}; safe to export.")


@cli.command("export-window")
@click.option("--start", "start_str", required=True, metavar="YYYY-MM-DD",
              help="Inclusive start transact date (UTC).")
@click.option("--end", "end_str", required=True, metavar="YYYY-MM-DD",
              help="Exclusive end transact date (UTC). Must be > --start.")
@click.option("--out-dir", type=click.Path(file_okay=False, path_type=Path), required=True,
              help="Directory for the parts and the export_window.jsonl ledger.")
@click.option("--tenant", default=None,
              help="HALO tenant to check coverage against (required unless "
                   "--skip-coverage-check).")
@click.option("--max-file-mb", type=click.FloatRange(min=1), default=window.DEFAULT_WINDOW_PART_MB,
              show_default=True, help="Per-part cap in MB (HALO rejects files over 500 MB).")
@click.option("--file-prefix", default=DEFAULT_FILE_PREFIX, show_default=True,
              help="Part-name prefix (production uses the tenant name).")
@click.option("--force", is_flag=True, default=False,
              help="Re-export days the ledger already marks ok.")
@click.option("--skip-coverage-check", is_flag=True, default=False,
              help="Do not ask the tenant what it holds (only when ClickHouse is unreachable "
                   "and the coverage is known).")
@click.option("--allow-covered", "allowed_str", multiple=True, metavar="YYYY-MM-DD",
              help="Export this day even though the tenant already holds rows for it "
                   "(repeatable; use after reading the coverage table).")
@_clickhouse_options
def export_window_cmd(
    start_str: str,
    end_str: str,
    out_dir: Path,
    tenant: str | None,
    max_file_mb: float,
    file_prefix: str,
    force: bool,
    skip_coverage_check: bool,
    allowed_str: tuple[str, ...],
    ch_env: str,
    ch_database: str,
    ch_script: Path | None,
    ch_python: str | None,
) -> None:
    """Export a window day by day in HALO-strict mode, gated on tenant coverage.

    Each day is its own query and its own part files; the outcome of every
    day is appended to export_window.jsonl in --out-dir, which upload-window
    reads. Re-running skips days already marked ok. Exit 1 if any day
    failed, 2 if the coverage check blocked the export.
    """
    start, end_exclusive = _window_dates(start_str, end_str)
    allowed = [_parse_date(value) for value in allowed_str]
    if skip_coverage_check:
        click.echo("WARNING: coverage check skipped; make sure none of these days is already "
                   "on the tenant.", err=True)
    else:
        if not tenant:
            raise click.UsageError("--tenant is required unless --skip-coverage-check is given.")
        report = _coverage_report(tenant, start, end_exclusive, ch_env, ch_database, ch_script,
                                  ch_python)
        blocked = report.blocked_days(allowed)
        if blocked:
            click.echo(
                f"Refusing to export {', '.join(d.isoformat() for d in blocked)}: already on "
                f"{tenant}. Pass --allow-covered <date> per day after checking the source files "
                "above, or ask Solidus for a purge first.",
                err=True,
            )
            raise SystemExit(2)

    def announce(record: window.DayRecord) -> None:
        if record.ok:
            click.echo(f"{record.day}: ok, {record.rows:,} rows in {len(record.parts)} part(s) "
                       f"({record.seconds / 60:.1f} min)")
        else:
            click.echo(f"{record.day}: {record.status.upper()} ({record.seconds / 60:.1f} min)\n"
                       f"{record.error}", err=True)

    click.echo(f"Exporting {start} .. {end_exclusive} day by day -> {out_dir} "
               f"({max_file_mb:g} MB parts, prefix {file_prefix})")
    result = window.export_window(start, end_exclusive, out_dir, max_part_mb=max_file_mb,
                                  file_prefix=file_prefix, force=force, on_day=announce)
    if result.skipped:
        click.echo(f"Skipped {len(result.skipped)} day(s) already exported: "
                   f"{', '.join(d.isoformat() for d in result.skipped)}")
    total_rows = sum(r.rows for r in result.records)
    click.echo(f"Done: {len(result.records) - len(result.failed)} day(s) exported "
               f"({total_rows:,} rows), {len(result.failed)} failed. Ledger: "
               f"{window.status_path(out_dir)}")
    if not result.ok:
        raise SystemExit(1)


@cli.command("upload-window")
@click.option("--start", "start_str", required=True, metavar="YYYY-MM-DD",
              help="Inclusive start transact date (UTC).")
@click.option("--end", "end_str", required=True, metavar="YYYY-MM-DD",
              help="Exclusive end transact date (UTC). Must be > --start.")
@click.option("--out-dir", type=click.Path(file_okay=False, exists=True, path_type=Path),
              required=True, help="Directory written by export-window.")
@click.option("--tenant", required=True,
              help="HALO tenant (HALO_API_KEY_<TENANT> in the halo-upload skill's .env).")
@click.option("--file-type", default=upload.DEFAULT_FILE_TYPE, show_default=True,
              help="HALO file type; the live route ingests automatically whatever the data age.")
@click.option("--region", default=upload.DEFAULT_REGION, show_default=True,
              help="HALO region slug.")
@click.option("--workers", type=click.IntRange(min=1), default=upload.DEFAULT_WORKERS,
              show_default=True, help="Parts uploaded in parallel.")
@click.option("--attempts", type=click.IntRange(min=1), default=upload.DEFAULT_ATTEMPTS,
              show_default=True, help="Tries per part before it is reported failed.")
@click.option("--retry-wait", type=click.FloatRange(min=0),
              default=upload.DEFAULT_RETRY_WAIT_SECONDS, show_default=True,
              help="Seconds between tries of the same part.")
@click.option("--poll", type=click.FloatRange(min=1), default=upload.DEFAULT_POLL_SECONDS,
              show_default=True, help="Seconds between ledger checks while an export is running.")
@click.option("--wait/--no-wait", default=True, show_default=True,
              help="Wait for days the export ledger does not list yet (export still running).")
@click.option("--dry-run", is_flag=True, default=False,
              help="List what would be sent and exit without contacting HALO.")
@click.option("--yes", "-y", is_flag=True, default=False,
              help="Skip the tenant confirmation prompt (for unattended runs).")
@click.option("--skill-script", type=click.Path(path_type=Path), default=None,
              help=f"Path of the halo-upload skill script "
                   f"(default: ${upload.SKILL_SCRIPT_ENV} or {upload.DEFAULT_SKILL_SCRIPT}).")
def upload_window_cmd(
    start_str: str,
    end_str: str,
    out_dir: Path,
    tenant: str,
    file_type: str,
    region: str,
    workers: int,
    attempts: int,
    retry_wait: float,
    poll: float,
    wait: bool,
    dry_run: bool,
    yes: bool,
    skill_script: Path | None,
) -> None:
    """Upload an exported window to a HALO tenant, never sending a part twice.

    Parts come from export_window.jsonl in --out-dir (only days marked ok).
    Every success is recorded in upload_<TENANT>.done at once, and a re-run
    skips those names. Exit 1 if any part failed or a day was skipped.
    """
    start, end_exclusive = _window_dates(start_str, end_str)
    try:
        settings = upload.UploadSettings(
            tenant=tenant.upper(), out_dir=out_dir,
            skill_script=upload.resolve_skill_script(skill_script),
            file_type=file_type, region=region, workers=workers, attempts=attempts,
            retry_wait_seconds=retry_wait, poll_seconds=poll,
        )
    except upload.UploadConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    days = list(window.iter_days(start, end_exclusive))
    click.echo(f"Tenant {settings.tenant} ({settings.region}, {settings.file_type}); "
               f"{len(days)} day(s) {start} .. {end_exclusive} from {out_dir}; "
               f"{settings.workers} worker(s), {settings.attempts} attempt(s) per part")
    if not dry_run and not yes:
        click.confirm(f"Upload to tenant {settings.tenant}?", abort=True)
    summary = upload.upload_window(settings, days, wait_for_export=wait, dry_run=dry_run,
                                   on_event=click.echo)
    if dry_run:
        click.echo(f"Dry run: {len(summary.planned)} part(s) would be sent, "
                   f"{len(summary.already_done)} already recorded as sent.")
        for name in summary.planned:
            click.echo(f"  {name}")
    else:
        click.echo(f"Done: {len(summary.sent)} sent, {len(summary.already_done)} already done, "
                   f"{len(summary.failed)} failed, {len(summary.skipped_days)} day(s) skipped.")
        for result in summary.failed:
            click.echo(f"  FAILED {result.name} after {result.attempts} attempt(s): "
                       f"{result.detail}", err=True)
    for skip in summary.skipped_days:
        click.echo(f"  SKIPPED {skip.day}: {skip.reason}", err=True)
    if not summary.ok:
        raise SystemExit(1)


def main() -> None:
    """Console-script entrypoint."""
    cli(obj={})


if __name__ == "__main__":
    sys.exit(main())
