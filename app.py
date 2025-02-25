import os
import requests
import datetime as dt
import pandas as pd
from flask import Flask, request, jsonify, render_template
from zoneinfo import ZoneInfo

app = Flask(__name__)

TIINGO_API_TOKEN = os.environ.get("TIINGO_API_TOKEN", "ff615c798ea9a36ef14889df10e184bd9de0d39d")

@app.route("/")
def index():
    """Serve the main HTML page."""
    return render_template("index.html")

@app.route("/api/historical")
def get_historical_data():
    """
    Returns a JSON with:
      - candles: user-chosen interval data
      - ema8, ema26, vwapGlobal, vwapDay (computed from user data)
      - dailySma50, dailySma100, dailySma200 (computed from 10 years of daily data)
    """
    symbol = request.args.get("symbol", "AAPL").upper()
    interval = request.args.get("interval", "1D")
    today = dt.date.today()

    # 1) Fetch user-chosen interval data
    if interval == "1D":
        start_date = today - dt.timedelta(days=3650)
        end_date = today
        user_url = (
            f"https://api.tiingo.com/tiingo/daily/{symbol}/prices?"
            f"startDate={start_date}&endDate={end_date}&token={TIINGO_API_TOKEN}"
        )
        parse_func_user = parse_daily_data
    else:
        start_date = today - dt.timedelta(days=30)
        end_date = today
        if interval == "1h":
            resample = "60min"
        else:
            resample = interval
        user_url = (
            f"https://api.tiingo.com/iex/{symbol}/prices?"
            f"startDate={start_date}&endDate={end_date}&resampleFreq={resample}"
            f"&columns=open,high,low,close,volume&token={TIINGO_API_TOKEN}"
        )
        parse_func_user = parse_intraday_data

    try:
        resp_user = requests.get(user_url)
        resp_user.raise_for_status()
        raw_data_user = resp_user.json()
    except Exception as e:
        return jsonify({"error": f"Failed user interval fetch: {e}"}), 400

    candle_list_user = parse_func_user(raw_data_user)
    if not candle_list_user:
        return jsonify({"error": "No data for user interval"}), 400

    # Convert to DataFrame to compute 8/26 EMA + VWAP
    df_user = pd.DataFrame(candle_list_user)
    df_user['datetime'] = pd.to_datetime(df_user['time'], unit='s', utc=True)
    df_user.set_index('datetime', inplace=True)
    df_user.sort_index(inplace=True)

    # 8 EMA
    df_user['ema8'] = df_user['close'].ewm(span=8, adjust=False).mean()

    # 26 EMA
    df_user['ema26'] = df_user['close'].ewm(span=26, adjust=False).mean()

    # Global VWAP
    df_user['typical_price'] = (df_user['high'] + df_user['low'] + df_user['close']) / 3
    df_user['cum_tp_vol'] = (df_user['typical_price'] * df_user['volume']).cumsum()
    df_user['cum_vol'] = df_user['volume'].cumsum()
    df_user['vwap_global'] = df_user['cum_tp_vol'] / df_user['cum_vol']

    # Daily VWAP (resets each day)
    df_user['date'] = df_user.index.date
    df_user['tp_x_vol'] = df_user['typical_price'] * df_user['volume']
    df_user['cum_tp_vol_day'] = df_user.groupby('date')['tp_x_vol'].cumsum()
    df_user['cum_vol_day'] = df_user.groupby('date')['volume'].cumsum()
    df_user['vwap_day'] = df_user['cum_tp_vol_day'] / df_user['cum_vol_day']

    df_user.reset_index(inplace=True)
    df_user['time'] = df_user['datetime'].astype(int) // 10**9

    # Build arrays
    user_candles = []
    ema8_data = []
    ema26_data = []
    vwap_global_data = []
    vwap_day_data = []

    for row in df_user.itertuples(index=False):
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
            ema26_data.append({"time": row.time, "value": round(row.ema26, 4)})
        if pd.notnull(row.vwap_global):
            vwap_global_data.append({"time": row.time, "value": round(row.vwap_global, 4)})
        if pd.notnull(row.vwap_day):
            vwap_day_data.append({"time": row.time, "value": round(row.vwap_day, 4)})

    # 2) Fetch 10 years of daily data for 50/100/200 SMAs
    daily_start = today - dt.timedelta(days=3650)
    daily_end = today
    daily_url = (
        f"https://api.tiingo.com/tiingo/daily/{symbol}/prices?"
        f"startDate={daily_start}&endDate={daily_end}"
        f"&columns=adjOpen,adjHigh,adjLow,adjClose,adjVolume,splitFactor"
        f"&token={TIINGO_API_TOKEN}"
    )
    try:
        resp_daily = requests.get(daily_url)
        resp_daily.raise_for_status()
        raw_data_daily = resp_daily.json()
    except Exception as e:
        return jsonify({"error": f"Failed daily fetch for SMAs: {e}"}), 400

    candle_list_daily = parse_daily_data(raw_data_daily)
    if not candle_list_daily:
        return jsonify({"error": "No daily data for SMAs"}), 400

    df_daily = pd.DataFrame(candle_list_daily)
    df_daily['datetime'] = pd.to_datetime(df_daily['time'], unit='s', utc=True)
    df_daily.set_index('datetime', inplace=True)
    df_daily.sort_index(inplace=True)

    # 50/100/200 SMAs
    df_daily['sma50'] = df_daily['close'].rolling(window=50).mean()
    df_daily['sma100'] = df_daily['close'].rolling(window=100).mean()
    df_daily['sma200'] = df_daily['close'].rolling(window=200).mean()

    df_daily.reset_index(inplace=True)
    df_daily['time'] = df_daily['datetime'].astype(int) // 10**9

    daily_sma50 = []
    daily_sma100 = []
    daily_sma200 = []

    for row in df_daily.itertuples(index=False):
        if pd.notnull(row.sma50):
            daily_sma50.append({"time": row.time, "value": round(row.sma50, 4)})
        if pd.notnull(row.sma100):
            daily_sma100.append({"time": row.time, "value": round(row.sma100, 4)})
        if pd.notnull(row.sma200):
            daily_sma200.append({"time": row.time, "value": round(row.sma200, 4)})

    return jsonify({
        "candles": user_candles,
        "ema8": ema8_data,
        "ema26": ema26_data,
        "vwapGlobal": vwap_global_data,
        "vwapDay": vwap_day_data,
        "dailySma50": daily_sma50,
        "dailySma100": daily_sma100,
        "dailySma200": daily_sma200
    })

def parse_daily_data(data):
    output = []
    for bar in data:
        date_str = bar.get("date")
        if not date_str:
            continue
        dt_utc = dt.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(ZoneInfo("America/Chicago"))
        timestamp = int(dt_local.timestamp())

        # Use adjusted fields for daily
        open_ = bar.get("adjOpen", 0)
        high_ = bar.get("adjHigh", 0)
        low_ = bar.get("adjLow", 0)
        close_ = bar.get("adjClose", 0)
        volume_ = bar.get("adjVolume", 0)

        output.append({
            "time": timestamp,
            "open": open_,
            "high": high_,
            "low": low_,
            "close": close_,
            "volume": volume_,
        })
    return output

def parse_intraday_data(data):
    output = []
    for bar in data:
        date_str = bar.get("date")
        if not date_str:
            continue
        dt_utc = dt.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(ZoneInfo("America/Chicago"))
        timestamp = int(dt_local.timestamp())

        # Intraday endpoint has only these raw fields
        open_ = bar.get("open", 0)
        high_ = bar.get("high", 0)
        low_ = bar.get("low", 0)
        close_ = bar.get("close", 0)
        volume_ = bar.get("volume", 0)

        output.append({
            "time": timestamp,
            "open": open_,
            "high": high_,
            "low": low_,
            "close": close_,
            "volume": volume_,
        })
    return output

if __name__ == "__main__":
    app.run(debug=True)
