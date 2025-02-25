import os
import sqlite3
import datetime as dt
import pandas as pd
from flask import Flask, request, jsonify, render_template
from zoneinfo import ZoneInfo

app = Flask(__name__)

DB_FILENAME = "stock_data.db"  # local SQLite DB


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/historical")
def get_historical_data():
    """
    Reads bars from the local DB and supports multiple intervals.
    - If interval = 1D, load daily bars and compute indicators.
    - If interval = 5min, 15min, 30min, or 1h, load 5m intraday bars and resample to the requested interval.
    Returns JSON with:
      candles, ema8, ema26, vwapDay, volume,
      dailySma50, dailySma100, dailySma200
    """
    symbol = request.args.get("symbol", "AAPL").upper()
    interval = request.args.get("interval", "1D")

    if interval == "1D":
        # Load daily bars
        bars = load_bars_from_db(symbol, interval="1D")
        if not bars:
            return jsonify({"error": f"No daily data in DB for {symbol}"}), 400

        # Compute indicators
        df = build_dataframe_and_compute_indicators(bars, daily=True)
        df["sma50"] = df["close"].rolling(window=50).mean()
        df["sma100"] = df["close"].rolling(window=100).mean()
        df["sma200"] = df["close"].rolling(window=200).mean()

        return jsonify(build_json_response(df, daily_sma=True))

    else:
        # Handle intraday intervals: 5min, 15min, 30min, 1h
        valid_intervals = {"5min": "5T", "15min": "15T", "30min": "30T", "1h": "1H"}
        if interval not in valid_intervals:
            return jsonify({"error": f"Unsupported interval: {interval}. Use 5min, 15min, 30min, or 1h"}), 400

        # Load 5m intraday bars as the base data
        bars_intraday = load_bars_from_db(symbol, interval="intraday")
        if not bars_intraday:
            return jsonify({"error": f"No intraday data in DB for {symbol}"}), 400

        # Build DataFrame and resample to the requested interval
        df_intraday = build_dataframe_and_compute_indicators(bars_intraday, daily=False)
        df_resampled = resample_intraday_data(df_intraday, valid_intervals[interval])

        # Load daily bars for SMAs
        bars_daily = load_bars_from_db(symbol, interval="1D")
        df_daily = None
        if bars_daily:
            df_daily = build_dataframe_and_compute_indicators(bars_daily, daily=True)
            df_daily["sma50"] = df_daily["close"].rolling(window=50).mean()
            df_daily["sma100"] = df_daily["close"].rolling(window=100).mean()
            df_daily["sma200"] = df_daily["close"].rolling(window=200).mean()

        return jsonify(build_json_response(df_resampled, daily_sma_df=df_daily))


@app.route("/api/anchored_vwap")
def get_anchored_vwap():
    """
    Calculate Anchored VWAP from a specific timestamp for the given symbol and interval.
    Returns JSON with 'anchoredVwap': [{'time': int, 'value': float}, ...].
    """
    symbol = request.args.get("symbol", "AAPL").upper()
    interval = request.args.get("interval", "1D")
    anchor_time = request.args.get("anchorTime")

    if not anchor_time:
        return jsonify({"error": "anchorTime parameter is required"}), 400

    anchor_time = int(anchor_time)  # Convert to integer (timestamp in seconds)

    # Load bars based on interval
    if interval == "1D":
        bars = load_bars_from_db(symbol, interval="1D")
        if not bars:
            return jsonify({"error": f"No daily data in DB for {symbol}"}), 400
        df = build_dataframe_and_compute_indicators(bars, daily=True)
    else:
        valid_intervals = {"5min": "5min", "15min": "15min", "30min": "30min", "1h": "1H"}
        if interval not in valid_intervals:
            return jsonify({"error": f"Unsupported interval: {interval}. Use 5min, 15min, 30min, or 1h"}), 400
        bars = load_bars_from_db(symbol, interval="intraday")
        if not bars:
            return jsonify({"error": f"No intraday data in DB for {symbol}"}), 400
        df_base = build_dataframe_and_compute_indicators(bars, daily=False)
        df = resample_intraday_data(df_base, valid_intervals[interval])

    # Filter data from anchor_time onward and compute Anchored VWAP
    anchor_dt = pd.to_datetime(anchor_time, unit='s', utc=True)  # Make anchor_time UTC-aware
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)  # Ensure datetime is UTC-aware
    df = df[df["datetime"] >= anchor_dt]
    if df.empty:
        return jsonify({"error": "No data available after the specified anchor time"}), 400

    df["typical_price"] = (df["high"] + df["low"] + df["close"]) / 3
    df["tp_x_vol"] = df["typical_price"] * df["volume"]
    df["cum_tp_vol"] = df["tp_x_vol"].cumsum()
    df["cum_vol"] = df["volume"].cumsum()
    df["vwap"] = df["cum_tp_vol"] / df["cum_vol"]

    # Prepare response
    df["time"] = df["datetime"].astype(int) // 10 ** 9
    anchored_vwap_data = [
        {"time": row.time, "value": round(row.vwap, 4)}
        for row in df.itertuples(index=False) if pd.notnull(row.vwap)
    ]

    return jsonify({"anchoredVwap": anchored_vwap_data})
def load_bars_from_db(symbol, interval="1D"):
    """
    Query local DB for the given symbol & interval.
    - "1D" => daily_bars
    - "intraday" => intraday_bars (assumed to be 5m data)
    Returns list of dicts: [{ 'timestamp': int, 'open':..., ... }, ...].
    """
    conn = sqlite3.connect(DB_FILENAME)
    cursor = conn.cursor()

    if interval == "1D":
        query = """SELECT timestamp, open, high, low, close, volume
                   FROM daily_bars
                   WHERE symbol=?
                   ORDER BY timestamp"""
    else:
        query = """SELECT timestamp, open, high, low, close, volume
                   FROM intraday_bars
                   WHERE symbol=?
                   ORDER BY timestamp"""

    rows = cursor.execute(query, (symbol,)).fetchall()
    conn.close()

    if not rows:
        return None

    bars = []
    for row in rows:
        bars.append({
            "timestamp": row[0],
            "open": row[1],
            "high": row[2],
            "low": row[3],
            "close": row[4],
            "volume": row[5],
        })
    return bars


def resample_intraday_data(df, interval):
    """
    Resample 5m intraday data to 15min, 30min, or 1h.
    - interval: '5min', '15min', '30min', '1H' (pandas resampling strings)
    Returns resampled DataFrame with recomputed indicators.
    """
    # Set datetime as index again since build_dataframe_and_compute_indicators resets it
    df = df.set_index("datetime")

    # Resample OHLCV
    df_resampled = df.resample(interval).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "tp_x_vol": "sum",
        "cum_vol_day": "sum"  # This will be recomputed
    }).dropna()

    # Recompute indicators for resampled data
    df_resampled["ema8"] = df_resampled["close"].ewm(span=8, adjust=False).mean()
    df_resampled["ema26"] = df_resampled["close"].ewm(span=26, adjust=False).mean()

    # Recompute VWAP per day
    df_resampled["typical_price"] = (df_resampled["high"] + df_resampled["low"] + df_resampled["close"]) / 3
    df_resampled["date"] = df_resampled.index.date
    df_resampled["tp_x_vol"] = df_resampled["typical_price"] * df_resampled["volume"]
    df_resampled["cum_tp_vol_day"] = df_resampled.groupby("date")["tp_x_vol"].cumsum()
    df_resampled["cum_vol_day"] = df_resampled.groupby("date")["volume"].cumsum()
    df_resampled["vwap_day"] = df_resampled["cum_tp_vol_day"] / df_resampled["cum_vol_day"]

    df_resampled.reset_index(inplace=True)
    return df_resampled


def build_dataframe_and_compute_indicators(bars, daily=True):
    """
    Convert bars to DataFrame, compute 8/26 EMA, vwapDay (volume-based).
    'daily=True' means each bar is a daily bar. 'daily=False' => intraday bars.
    """
    df = pd.DataFrame(bars)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df.set_index("datetime", inplace=True)
    df.sort_index(inplace=True)

    # 8/26 EMA
    df["ema8"] = df["close"].ewm(span=8, adjust=False).mean()
    df["ema26"] = df["close"].ewm(span=26, adjust=False).mean()

    # Daily VWAP
    df["typical_price"] = (df["high"] + df["low"] + df["close"]) / 3
    df["date"] = df.index.date
    df["tp_x_vol"] = df["typical_price"] * df["volume"]
    df["cum_tp_vol_day"] = df.groupby("date")["tp_x_vol"].cumsum()
    df["cum_vol_day"] = df.groupby("date")["volume"].cumsum()
    df["vwap_day"] = df["cum_tp_vol_day"] / df["cum_vol_day"]

    df.reset_index(inplace=True)
    return df


def build_json_response(df, daily_sma=False, daily_sma_df=None):
    """
    Build final JSON:
      - 'candles' for the main bars
      - 'ema8', 'ema26', 'vwapDay'
      - 'volume'
      - 'dailySma50', 'dailySma100', 'dailySma200' (from same DF if daily_sma=True, or from daily_sma_df if intraday)
    """
    df["time"] = df["datetime"].astype(int) // 10 ** 9
    df.sort_values("time", inplace=True)

    user_candles = []
    ema8_data = []
    ema26_data = []
    vwap_day_data = []
    volume_data = []

    for row in df.itertuples(index=False):
        user_candles.append({
            "time": row.time,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume
        })
        if pd.notnull(row.ema8):
            ema8_data.append({"time": row.time, "value": round(row.ema8, 4)})
        if pd.notnull(row.ema26):
            ema26_data.append({"time": row.time, "value": round(row.ema26, 4)})  # Fixed typo here (was ema8_data)
        if pd.notnull(row.vwap_day):
            vwap_day_data.append({"time": row.time, "value": round(row.vwap_day, 4)})
        volume_data.append({"time": row.time, "value": row.volume})

    daily_sma50 = []
    daily_sma100 = []
    daily_sma200 = []

    if daily_sma and "sma50" in df.columns:
        for row in df.itertuples(index=False):
            if pd.notnull(row.sma50):
                daily_sma50.append({"time": row.time, "value": round(row.sma50, 4)})
            if pd.notnull(row.sma100):
                daily_sma100.append({"time": row.time, "value": round(row.sma100, 4)})
            if pd.notnull(row.sma200):
                daily_sma200.append({"time": row.time, "value": round(row.sma200, 4)})

    if daily_sma_df is not None:
        daily_sma_df["time"] = daily_sma_df["datetime"].astype(int) // 10 ** 9
        daily_sma_df.sort_values("time", inplace=True)
        for row in daily_sma_df.itertuples(index=False):
            if pd.notnull(row.sma50):
                daily_sma50.append({"time": row.time, "value": round(row.sma50, 4)})
            if pd.notnull(row.sma100):
                daily_sma100.append({"time": row.time, "value": round(row.sma100, 4)})
            if pd.notnull(row.sma200):
                daily_sma200.append({"time": row.time, "value": round(row.sma200, 4)})

    return {
        "candles": user_candles,
        "ema8": ema8_data,
        "ema26": ema26_data,
        "vwapDay": vwap_day_data,
        "volume": volume_data,
        "dailySma50": daily_sma50,
        "dailySma100": daily_sma100,
        "dailySma200": daily_sma200
    }


if __name__ == "__main__":
    app.run(debug=True)
