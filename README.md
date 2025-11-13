# Hyperliquid Historical L2 Orderbook Downloader

A CLI tool for downloading tick-level historical L2 orderbook snapshots from the Hyperliquid exchange, decompressing them, and converting them to CSV.

Data is fetched from the public S3 bucket described in the Hyperliquid docs:

- Bucket: `hyperliquid-archive`
- L2 book snapshots: `s3://hyperliquid-archive/market_data/[date]/[hour]/l2Book/[coin].lz4`

> **Note:** The bucket is requester-pays. You must have AWS credentials configured, and the tool will send `RequestPayer=requester` on downloads.

## Requirements

- Python 3.10 or above
- pip
- AWS credentials with access to requester-pays buckets

Install Python dependencies from `requirements.txt`:

```bash
pip install -r requirements.txt
```

Downloaded and decompressed files for a given asset are stored under `./downloads/{asset}/`.
CSVs are stored under `./csv/{asset}/`.

---

## Usage

Basic command format:

```bash
python hyperliquid-historical.py {global_settings,download,decompress,to_csv} ...
```

The CLI mirrors the original script layout while using the current S3 path structure.

### Top-level usage

```text
usage: hyperliquid-historical.py [-h] {global_settings,download,decompress,to_csv} ...

Retrieve historical tick level market data from Hyperliquid exchange

positional arguments:
  {global_settings,download,decompress,to_csv}
                        tool: download, decompress, to_csv
    download            Download historical market data
    decompress          Decompress downloaded lz4 data
    to_csv              Convert decompressed downloads into formatted CSV

options:
  -h, --help            show this help message and exit
```

---

### Download

```text
usage: hyperliquid-historical.py download [-h] [--all] [-sd Start date] [-sh Start hour] [-ed End date] [-eh End hour]
                                          Tickers [Tickers ...]

positional arguments:
  Tickers         Tickers of assets to be downloaded separated by spaces. e.g. BTC ETH

options:
  -h, --help      show this help message and exit
  --all           Apply action to all available dates and times (not implemented).
  -sd Start date  Starting date as one unbroken string formatted: YYYYMMDD. e.g. 20230916
  -sh Start hour  Hour of the starting day as an integer between 0 and 23. Default: 0
  -ed End date    Ending date as one unbroken string formatted: YYYYMMDD. e.g. 20230916
  -eh End hour    Hour of the ending day as an integer between 0 and 23. Default: 23
```

Example: download BTC and ETH L2 snapshots for the full day 2024-01-01:

```bash
python hyperliquid-historical.py download BTC ETH -sd 20240101 -sh 0 -ed 20240101 -eh 23
```

Example: download a single hour for SOL (matching the docs example):

```bash
python hyperliquid-historical.py download SOL -sd 20230916 -sh 9 -ed 20230916 -eh 9
```

---

### Decompress

```text
usage: hyperliquid-historical.py decompress [-h] [--all] [-sd Start date] [-sh Start hour] [-ed End date]
                                            [-eh End hour]
                                            Tickers [Tickers ...]

positional arguments:
  Tickers         Tickers of assets to be decompressed separated by spaces. e.g. BTC ETH

options:
  -h, --help      show this help message and exit
  --all           Apply action to all available dates and times (not implemented).
  -sd Start date  Starting date as one unbroken string formatted: YYYYMMDD. e.g. 20230916
  -sh Start hour  Hour of the starting day as an integer between 0 and 23. Default: 0
  -ed End date    Ending date as one unbroken string formatted: YYYYMMDD. e.g. 20230916
  -eh End hour    Hour of the ending day as an integer between 0 and 23. Default: 23
```

Example: decompress BTC lz4 files for 2024-01-01:

```bash
python hyperliquid-historical.py decompress BTC -sd 20240101 -sh 0 -ed 20240101 -eh 23
```

This expects `.lz4` files to exist under `./downloads/BTC/` with names like `YYYYMMDD-HH.lz4`.

---

### Convert to CSV

```text
usage: hyperliquid-historical.py to_csv [-h] [--all] [-sd Start date] [-sh Start hour] [-ed End date] [-eh End hour]
                                        Tickers [Tickers ...]

positional arguments:
  Tickers         Tickers of assets to be converted to CSV separated by spaces. e.g. BTC ETH

options:
  -h, --help      show this help message and exit
  --all           Apply action to all available dates and times (not implemented).
  -sd Start date  Starting date as one unbroken string formatted: YYYYMMDD. e.g. 20230916
  -sh Start hour  Hour of the starting day as an integer between 0 and 23. Default: 0
  -ed End date    Ending date as one unbroken string formatted: YYYYMMDD. e.g. 20230916
  -eh End hour    Hour of the ending day as an integer between 0 and 23. Default: 23
```

Example: convert BTC data for 2024-01-01 to CSV:

```bash
python hyperliquid-historical.py to_csv BTC -sd 20240101 -sh 0 -ed 20240101 -eh 23
```

This expects decompressed text files under `./downloads/BTC/` with names like `YYYYMMDD-HH` (no extension).

---

## AWS requester-pays

The Hyperliquid archive bucket is configured as requester-pays. This tool:

- Uses an unsigned S3 client for convenience.
- Sends `RequestPayer=requester` on each `download_file` call.

You must have valid AWS credentials in your environment (e.g. via `~/.aws/credentials`, environment variables, or an IAM role) that are allowed to access requester-pays buckets.

If you receive `403 Forbidden` errors when downloading, check:

- Your AWS credentials are configured and active.
- The specific `[date]/[hour]/l2Book/[coin].lz4` objects you are requesting actually exist (some dates/markets may be missing as per Hyperliquid docs).
