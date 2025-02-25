import os
import sqlite3
import logging
import requests
import datetime as dt
from zoneinfo import ZoneInfo
from tickers import TICKERS  # import your ticker list

# Use your Tiingo API token here or from environment
TIINGO_API_TOKEN = os.environ.get("TIINGO_API_TOKEN", "ff615c798ea9a36ef14889df10e184bd9de0d39d")

DB_FILENAME = "stock_data.db"

def create_tables(conn):
    """
    Create table for intraday bars if it doesn't exist.
    """
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS intraday_bars (
        symbol TEXT,
        timestamp INTEGER,
        open REAL,
        high REAL,
        low REAL,
        close REAL,
        volume REAL,
        PRIMARY KEY (symbol, timestamp)
    )
    """)
    conn.commit()

def parse_intraday_data(data):
    """
    Parses Tiingo intraday data (raw fields) into a list of dicts:
    [{ 'timestamp': int, 'open': float, 'high': float, 'low': float, 'close': float, 'volume': float }, ...]
    """
    output = []
    for bar in data:
        date_str = bar.get("date")
        if not date_str:
            continue
        dt_utc = dt.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(ZoneInfo("America/Chicago"))
        timestamp = int(dt_local.timestamp())

        open_ = bar.get("open", 0)
        high_ = bar.get("high", 0)
        low_ = bar.get("low", 0)
        close_ = bar.get("close", 0)
        volume_ = bar.get("volume", 0)

        output.append({
            "timestamp": timestamp,
            "open": open_,
            "high": high_,
            "low": low_,
            "close": close_,
            "volume": volume_,
        })
    return output

def store_intraday_bars(conn, symbol, bars):
    """
    Inserts intraday bars into the 'intraday_bars' table.
    """
    cursor = conn.cursor()
    for bar in bars:
        cursor.execute("""
            INSERT OR IGNORE INTO intraday_bars (symbol, timestamp, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol,
            bar["timestamp"],
            bar["open"],
            bar["high"],
            bar["low"],
            bar["close"],
            bar["volume"]
        ))
    conn.commit()

def fetch_intraday_bars(symbol):
    """
    Fetch 365 days of 5-minute intraday data for the given symbol.
    Returns a list of parsed intraday bars.
    """
    today = dt.date.today()
    start_date = today - dt.timedelta(days=365)  # Changed from 30 to 365 days
    end_date = today

    resample = "5min"
    url = (
        f"https://api.tiingo.com/iex/{symbol}/prices?"
        f"startDate={start_date}&endDate={end_date}"
        f"&resampleFreq={resample}&columns=open,high,low,close,volume"
        f"&token={TIINGO_API_TOKEN}"
    )
    logging.debug(f"Fetching {resample} intraday bars for {symbol} from {start_date} to {end_date}")
    resp = requests.get(url)
    resp.raise_for_status()
    data = resp.json()
    return parse_intraday_data(data)

def main():
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Connect to DB
    conn = sqlite3.connect(DB_FILENAME)
    create_tables(conn)

    total_tickers = len(TICKERS)
    logging.info(f"Starting update for {total_tickers} tickers...")

    for i, symbol in enumerate(TICKERS):
        logging.info(f"({i+1}/{total_tickers}) Fetching intraday bars for {symbol}...")
        try:
            intraday_bars = fetch_intraday_bars(symbol)
            store_intraday_bars(conn, symbol, intraday_bars)
            logging.info(f"Stored {len(intraday_bars)} intraday bars for {symbol}.")
        except Exception as e:
            logging.error(f"Error fetching intraday for {symbol}: {e}")

    conn.close()
    logging.info("Update complete. All tickers processed.")

if __name__ == "__main__":
    main()
