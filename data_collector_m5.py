import MetaTrader5 as mt5
import pandas as pd

SYMBOL    = "US30"
TIMEFRAME = mt5.TIMEFRAME_M5
BARS      = 100   # Aurora foloseste 5-15 bare — 100 e mai mult decat suficient

def get_data():
    try:
        rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, 0, BARS)
        if rates is None or len(rates) == 0:
            return None
        df = pd.DataFrame(rates)
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], unit="s")
        df = df.rename(columns={"tick_volume": "volume"})
        df = df.dropna()
        return df
    except Exception as e:
        print(f"  [DATA ERROR] {e}")
        return None
