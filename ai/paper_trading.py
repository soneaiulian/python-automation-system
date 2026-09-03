import csv
from datetime import datetime

balance = 1000
risk_per_trade = 10

trades = []


def open_trade(direction, entry, sl, tp):
    return {
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "active": True,
        "open_time": datetime.now()
    }


def check_trade(trade, price):
    global balance

    if not trade["active"]:
        return None

    # 🔥 simulare spread
    price = price - 0.2

    result = None

    if trade["direction"] == "LONG":
        if price <= trade["sl"]:
            balance -= risk_per_trade
            result = "LOSS"

        elif price >= trade["tp"]:
            balance += risk_per_trade * 2
            result = "WIN"

    elif trade["direction"] == "SHORT":
        if price >= trade["sl"]:
            balance -= risk_per_trade
            result = "LOSS"

        elif price <= trade["tp"]:
            balance += risk_per_trade * 2
            result = "WIN"

    if result:
        trade["active"] = False
        trade["exit"] = price
        trade["result"] = result
        trade["close_time"] = datetime.now()

        trades.append(trade)
        save_trade(trade)

    return result


def save_trade(trade):
    with open("trades_log.csv", "a", newline="") as f:
        writer = csv.writer(f)

        writer.writerow([
            trade["open_time"],
            trade["close_time"],
            trade["direction"],
            trade["entry"],
            trade["exit"],
            trade["result"]
        ])


def log_trade(result, price):
    print(f"📊 Trade {result} @ {price} | Balance: {balance}")