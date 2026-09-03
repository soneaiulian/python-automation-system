def check_reentry(last_trade, df, aurora):

    if not last_trade or not aurora:
        return None

    bias = aurora["bias"]

    prices = df["close"].values

    if len(prices) < 5:
        return None

    last = prices[-1]
    prev = prices[-2]
    prev2 = prices[-3]

    move_now = last - prev

    last_direction = last_trade["signal"]

    # =========================
    # LONG RE-ENTRY
    # =========================
    if bias == "LONG" and last_direction == "BUY":

        # pullback după exit
        if prev < prev2:

            # revenire
            if move_now > 0:
                return "BUY"

    # =========================
    # SHORT RE-ENTRY
    # =========================
    if bias == "SHORT" and last_direction == "SELL":

        if prev > prev2:

            if move_now < 0:
                return "SELL"

    return None