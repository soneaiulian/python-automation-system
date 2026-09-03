def should_exit(trade, df):

    prices = df["close"].values

    if len(prices) < 5:
        return False

    last = prices[-1]
    prev = prices[-2]
    prev2 = prices[-3]

    move_now = last - prev
    move_prev = prev - prev2

    entry = trade["entry_price"]
    direction = trade["signal"]

    # =========================
    # PROFIT
    # =========================
    if direction == "BUY":
        profit = last - entry
    else:
        profit = entry - last

    # =========================
    # 🔴 1. HARD STOP (pierdere)
    # =========================
    if profit < -1.5:
        return True

    # =========================
    # 🟡 2. EARLY PROTECT
    # =========================
    if profit > 0.8:
        if abs(move_now) < abs(move_prev):
            return True

    # =========================
    # 🔥 3. TRAILING PROFIT (IMPORTANT)
    # =========================
    if profit > 1.5:

        # dacă momentum scade → ieși
        if abs(move_now) < abs(move_prev):
            return True

    # =========================
    # 🔴 4. REVERSAL DETECTION
    # =========================
    if direction == "BUY":
        if move_now < 0 and move_prev > 0:
            return True

    if direction == "SELL":
        if move_now > 0 and move_prev < 0:
            return True

    # =========================
    # 🟢 5. TREND HOLD (NU IEȘI PREA DEVREME)
    # =========================
    if profit > 2.0:

        if direction == "BUY" and move_now > 0:
            return False

        if direction == "SELL" and move_now < 0:
            return False

    return False