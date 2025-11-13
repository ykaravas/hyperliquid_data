import argparse
import asyncio
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List

import boto3


# Base directory for all downloads and CSV output
DIR_PATH: Path = Path(__file__).parent
BUCKET: str = "hyperliquid-archive"
CSV_HEADER: List[str] = ["datetime", "timestamp", "level", "price", "size", "number"]


@dataclass
class CliArgs:
    """Container for parsed CLI arguments.

    Attributes:
        tool: The subcommand to run (download, decompress, to_csv).
        tickers: List of asset tickers to process.
        all: If True, operate on all available dates/hours (currently not implemented).
        sd: Start date in YYYYMMDD format.
        sh: Start hour (0-23) for the start date.
        ed: End date in YYYYMMDD format.
        eh: End hour (0-23) for the end date.
    """

    tool: str
    tickers: List[str]
    all: bool
    sd: str
    sh: int
    ed: str
    eh: int


def get_args() -> CliArgs:
    """Parse command line arguments into a `CliArgs` instance.

    Returns:
        Parsed command line arguments.
    """

    parser = argparse.ArgumentParser(
        description="Retrieve historical tick level market data from Hyperliquid exchange",
    )
    subparser = parser.add_subparsers(
        dest="tool",
        required=True,
        help="tool: download, decompress, to_csv",
    )

    global_parser = subparser.add_parser("global_settings", add_help=False)
    global_parser.add_argument(
        "t",
        metavar="Tickers",
        help="Tickers of assets to be handled separated by spaces. e.g. BTC ETH",
        nargs="+",
    )
    global_parser.add_argument(
        "--all",
        help="Apply action to all available dates and times (not implemented).",
        action="store_true",
        default=False,
    )
    global_parser.add_argument(
        "-sd",
        metavar="Start date",
        help="Starting date as YYYYMMDD. e.g. 20230916",
        required=True,
    )
    global_parser.add_argument(
        "-sh",
        metavar="Start hour",
        help="Hour of the starting day as integer between 0 and 23. Default: 0",
        type=int,
        default=0,
    )
    global_parser.add_argument(
        "-ed",
        metavar="End date",
        help="Ending date as YYYYMMDD. e.g. 20230916",
        required=True,
    )
    global_parser.add_argument(
        "-eh",
        metavar="End hour",
        help="Hour of the ending day as integer between 0 and 23. Default: 23",
        type=int,
        default=23,
    )

    subparser.add_parser(
        "download",
        help="Download historical market data",
        parents=[global_parser],
    )
    subparser.add_parser(
        "decompress",
        help="Decompress downloaded lz4 data",
        parents=[global_parser],
    )
    subparser.add_parser(
        "to_csv",
        help="Convert decompressed downloads into formatted CSV",
        parents=[global_parser],
    )

    args = parser.parse_args()
    return CliArgs(
        tool=args.tool,
        tickers=args.t,
        all=args.all,
        sd=args.sd,
        sh=args.sh,
        ed=args.ed,
        eh=args.eh,
    )


def make_date_list(start_date: str, end_date: str) -> List[str]:
    """Create a list of date strings between start and end inclusive.

    Args:
        start_date: Start date in YYYYMMDD format.
        end_date: End date in YYYYMMDD format.

    Returns:
        List of dates as YYYYMMDD strings.
    """

    start = datetime.strptime(start_date, "%Y%m%d")
    end = datetime.strptime(end_date, "%Y%m%d")

    dates: List[str] = []
    current = start
    while current <= end:
        dates.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)

    return dates


def make_date_hour_list(
    date_list: Iterable[str],
    start_hour: int,
    end_hour: int,
    delimiter: str = "/",
) -> List[str]:
    """Create combined date/hour strings for the requested range.

    Args:
        date_list: Iterable of date strings in YYYYMMDD format.
        start_hour: Hour to start from on the first date (0-23).
        end_hour: Hour to end on the last date (0-23).
        delimiter: Delimiter between date and hour in the resulting strings.

    Returns:
        List of strings in the form YYYYMMDD{delimiter}H.
    """

    dates = list(date_list)
    if not dates:
        return []

    result: List[str] = []
    last_date = dates[-1]

    hour = start_hour
    day_end = 23
    for date in dates:
        if date == last_date:
            day_end = end_hour

        while hour <= day_end:
            result.append(f"{date}{delimiter}{hour}")
            hour += 1

        hour = 0

    return result


async def download_object(s3_client, asset: str, date_hour: str) -> None:
    """Download a single l2Book snapshot object from S3.

    Args:
        s3_client: Boto3 S3 client instance.
        asset: Asset ticker used in the S3 key (e.g. BTC, SOL, POPCAT-USDC).
        date_hour: String of the form YYYYMMDD/HH representing date and hour.
    """

    date_str, hour_str = date_hour.split("/")
    key = f"market_data/{date_str}/{hour_str}/l2Book/{asset}.lz4"
    target_dir = DIR_PATH / "downloads" / asset
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{date_str}-{hour_str}.lz4"

    try:
        s3_client.download_file(
            BUCKET,
            key,
            str(target_path),
            ExtraArgs={"RequestPayer": "requester"},
        )
        print(f"Downloaded {key} -> {target_path}")
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to download {key}: {exc}")


async def download_objects(s3_client, assets: Iterable[str], date_hour_list: Iterable[str]) -> None:
    """Download all requested objects for all assets and date-hours.

    Args:
        s3_client: Boto3 S3 client instance.
        assets: Iterable of asset tickers to download.
        date_hour_list: Iterable of date/hour strings in YYYYMMDD/HH format.
    """

    date_hours = list(date_hour_list)
    print(f"Downloading {len(date_hours)} objects per asset...")

    for asset in assets:
        await asyncio.gather(
            *[download_object(s3_client, asset, date_hour) for date_hour in date_hours],
        )


async def decompress_file(asset: str, date_hour: str) -> None:
    """Decompress a single lz4 file for an asset and date/hour.

    Args:
        asset: Asset ticker.
        date_hour: Date/hour identifier used in the filename (YYYYMMDD-HH).
    """

    import lz4.frame

    lz_file_path = DIR_PATH / "downloads" / asset / f"{date_hour}.lz4"
    file_path = DIR_PATH / "downloads" / asset / date_hour

    if not lz_file_path.is_file():
        print(f"decompress_file: file not found: {lz_file_path}")
        return

    with lz4.frame.open(lz_file_path, mode="r") as lzfile:
        data = lzfile.read()
        with open(file_path, "wb") as file:
            file.write(data)

    print(f"Decompressed {lz_file_path} -> {file_path}")


async def decompress_files(assets: Iterable[str], date_hour_list: Iterable[str]) -> None:
    """Decompress all lz4 files for the given assets and date-hours.

    Args:
        assets: Iterable of asset tickers.
        date_hour_list: Iterable of date/hour identifiers (YYYYMMDD-HH).
    """

    date_hours = list(date_hour_list)
    print(f"Decompressing {len(date_hours)} files per asset...")

    for asset in assets:
        await asyncio.gather(
            *[decompress_file(asset, date_hour) for date_hour in date_hours],
        )


def write_rows(csv_writer: csv.writer, line: str) -> None:
    """Parse one JSON line of orderbook data and write rows to CSV.

    Args:
        csv_writer: CSV writer used to write rows.
        line: Single line of JSON from the decompressed file.
    """

    entry = json.loads(line)
    date_time = entry["time"]
    timestamp = str(entry["raw"]["data"]["time"])
    all_orders = entry["raw"]["data"]["levels"]

    for level_index, order_level in enumerate(all_orders, start=1):
        level = str(level_index)
        for order in order_level:
            price = order["px"]
            size = order["sz"]
            number = str(order["n"])
            csv_writer.writerow([date_time, timestamp, level, price, size, number])


async def convert_file(asset: str, date_hour: str) -> None:
    """Convert one decompressed text file to CSV.

    Args:
        asset: Asset ticker.
        date_hour: Date/hour identifier used in the filename (YYYYMMDD-HH).
    """

    file_path = DIR_PATH / "downloads" / asset / date_hour
    csv_dir = DIR_PATH / "csv" / asset
    csv_dir.mkdir(parents=True, exist_ok=True)
    csv_path = csv_dir / f"{date_hour}.csv"

    with open(csv_path, "w", newline="") as csv_file:
        csv_writer = csv.writer(csv_file, dialect="excel")
        csv_writer.writerow(CSV_HEADER)

        with open(file_path) as file:
            for line in file:
                write_rows(csv_writer, line)

    print(f"Converted {file_path} -> {csv_path}")


async def files_to_csv(assets: Iterable[str], date_hour_list: Iterable[str]) -> None:
    """Convert all decompressed files to CSV for the given assets and date-hours.

    Args:
        assets: Iterable of asset tickers.
        date_hour_list: Iterable of date/hour identifiers (YYYYMMDD-HH).
    """

    date_hours = list(date_hour_list)
    print(f"Converting {len(date_hours)} files per asset to CSV...")

    for asset in assets:
        await asyncio.gather(
            *[convert_file(asset, date_hour) for date_hour in date_hours],
        )


def main() -> None:
    """Entrypoint for the CLI tool.

    This function:

    - Parses CLI arguments.
    - Builds date/hour ranges.
    - Creates necessary directories.
    - Runs the selected subcommand (download, decompress, to_csv).
    """

    print(DIR_PATH)

    s3 = boto3.client("s3")
    args = get_args()

    downloads_path = DIR_PATH / "downloads"
    downloads_path.mkdir(exist_ok=True)

    csv_base_path = DIR_PATH / "csv"
    csv_base_path.mkdir(exist_ok=True)

    for asset in args.tickers:
        (downloads_path / asset).mkdir(exist_ok=True)
        (csv_base_path / asset).mkdir(exist_ok=True)

    date_list = make_date_list(args.sd, args.ed)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        if args.tool == "download":
            date_hour_list = make_date_hour_list(date_list, args.sh, args.eh, delimiter="/")
            loop.run_until_complete(download_objects(s3, args.tickers, date_hour_list))

        elif args.tool == "decompress":
            date_hour_list = make_date_hour_list(date_list, args.sh, args.eh, delimiter="-")
            loop.run_until_complete(decompress_files(args.tickers, date_hour_list))

        elif args.tool == "to_csv":
            date_hour_list = make_date_hour_list(date_list, args.sh, args.eh, delimiter="-")
            loop.run_until_complete(files_to_csv(args.tickers, date_hour_list))

    finally:
        loop.close()

    print("Done")


if __name__ == "__main__":
    main()
