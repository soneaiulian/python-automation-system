"""
features_us30.py
=================
Feature engineering pentru US30 (Wall Street 30).

Diferente fata de Gold M15:
  - RSI overbought/oversold: 70/30 (Gold folosea 72/28)
  - overextended: 0.0015 (Gold 0.0025) — US30 e mai volatil ca pct din pret
  - reversal RSI: 45/55 (Gold 42/58) — US30 revine mai rapid
  - volatility_spike: 1.8x (Gold 1.5x) — US30 are spike-uri mai frecvente
  - adaugat: gap detector (US30 deschide cu gap frecvent)
  - adaugat: session_momentum (NYSE open 15:30-17:00 = miscare mare)
"""

def add_features(df):
    import ta
    import numpy as np
    import pandas as pd

    # =========================
    # BASIC
    # =========================
    df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()

    df['ma_fast'] = df['close'].rolling(10).mean()
    df['ma_slow'] = df['close'].rolling(50).mean()
    df['ma'] = df['ma_slow']

    # =========================
    # TREND
    # =========================
    df['trend']           = df['ma_fast'] - df['ma_slow']
    df['trend_strength']  = abs(df['trend'])
    df['trend_direction'] = np.sign(df['trend'])

    df['trend_acceleration'] = df['trend'] - df['trend'].shift(3)

    # =========================
    # VOLATILITY
    # US30: spike threshold 1.8x (mai frecvent decat Gold)
    # =========================
    df['volatility']      = df['high'] - df['low']
    df['volatility_norm'] = df['volatility'] / df['close']

    df['volatility_ma']    = df['volatility'].rolling(20).mean()
    df['volatility_spike'] = (df['volatility'] > df['volatility_ma'] * 1.8).astype(int)

    df['range_compression'] = df['volatility'] / (df['volatility_ma'] + 1e-6)

    # =========================
    # GAP DETECTOR
    # US30 deschide frecvent cu gap vs inchiderea precedenta
    # =========================
    df['gap'] = df['open'] - df['close'].shift(1)
    df['gap_pct'] = df['gap'] / df['close'].shift(1)
    df['gap_up']   = (df['gap_pct'] >  0.0003).astype(int)   # gap > 0.03%
    df['gap_down'] = (df['gap_pct'] < -0.0003).astype(int)

    # =========================
    # MOMENTUM
    # =========================
    df['momentum']     = df['close'] - df['close'].shift(5)
    df['momentum_pct'] = df['momentum'] / df['close']
    df['momentum_acc'] = df['momentum'] - df['momentum'].shift(3)

    # =========================
    # DISTANCE FROM MEAN
    # US30: 0.0015 (Gold era 0.0025)
    # La US30 ~43000 → 0.0015 * 43000 = 64.5 puncte departe de MA
    # =========================
    df['distance_ma']     = df['close'] - df['ma']
    df['distance_ma_pct'] = df['distance_ma'] / df['close']

    # =========================
    # TREND vs VOL
    # =========================
    df['trend_vol_ratio'] = df['trend'] / (df['volatility'] + 1e-6)

    # =========================
    # BREAKOUT
    # =========================
    df['high_break']     = df['close'] - df['high'].shift(10)
    df['low_break']      = df['close'] - df['low'].shift(10)
    df['high_break_pct'] = df['high_break'] / df['close']
    df['low_break_pct']  = df['low_break']  / df['close']

    # =========================
    # CANDLES
    # =========================
    df['candle_body']      = df['close'] - df['open']
    df['candle_range']     = df['high']   - df['low']
    df['candle_strength']  = df['candle_body'] / (df['candle_range'] + 1e-6)
    df['candle_direction'] = np.sign(df['candle_body'])

    # =========================
    # RSI DYNAMICS
    # =========================
    df['rsi_trend'] = df['rsi'] - df['rsi'].shift(3)

    # =========================
    # EXTREME CONDITIONS — US30
    # Gold: 72/28  →  US30: 70/30
    # =========================
    df['rsi_overbought'] = (df['rsi'] > 70).astype(int)
    df['rsi_oversold']   = (df['rsi'] < 30).astype(int)

    # =========================
    # OVEREXTENDED — US30
    # Gold: 0.0025  →  US30: 0.0015
    # US30 ~43000 → threshold ~64 puncte de la MA
    # =========================
    df['overextended_up']   = (df['distance_ma_pct'] >  0.0015).astype(int)
    df['overextended_down'] = (df['distance_ma_pct'] < -0.0015).astype(int)

    # =========================
    # SESSION MOMENTUM
    # NYSE open 15:30-17:00 Romania = miscare mare pe US30
    # Pre-market 14:30-15:30 = setup
    # =========================
    if hasattr(df.index, 'hour'):
        hour = df.index.hour
    elif 'time' in df.columns:
        hour = pd.to_datetime(df['time']).dt.hour
    else:
        hour = pd.Series([0] * len(df), index=df.index)

    df['nyse_open']    = ((hour >= 15) & (hour < 17)).astype(int)   # prima ora NYSE
    df['nyse_active']  = ((hour >= 15) & (hour < 22)).astype(int)   # sesiune completa
    df['premarket']    = ((hour >= 14) & (hour < 15)).astype(int)

    # =========================
    # REVERSAL LOGIC — US30
    # RSI thresholds: 45/55 (Gold: 42/58)
    # US30 revine mai rapid → praguri mai aproape de centru
    # =========================
    df['reversal_up'] = (
        (df['momentum']     > 0) &
        (df['momentum_acc'] > 0) &
        (df['rsi']          < 45) &
        (df['volatility_spike'] == 1)
    ).astype(int)

    df['reversal_down'] = (
        (df['momentum']     < 0) &
        (df['momentum_acc'] < 0) &
        (df['rsi']          > 55) &
        (df['volatility_spike'] == 1)
    ).astype(int)

    # =========================
    # CLEAN
    # =========================
    return df.dropna()
