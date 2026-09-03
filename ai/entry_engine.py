import time

# 🔥 CONFIG
CONFIRMATION_TICKS = 3   # câte confirmări cerem
last_prices = []
active_signal = None
confirmation_count = 0


def update_price(price):
    global last_prices

    last_prices.append(price)

    # păstrăm doar ultimele 3
    if len(last_prices) > 3:
        last_prices.pop(0)


def detect_structure():
    """
    Detectează HH / LL simplu
    """
    if len(last_prices) < 3:
        return None

    p1, p2, p3 = last_prices

    # Higher High
    if p3 > p2 and p2 >= p1:
        return "HH"

    # Lower Low
    if p3 < p2 and p2 <= p1:
        return "LL"

    return None


def process_signal(signal, current_price):
    global active_signal, confirmation_count

    update_price(current_price)
    structure = detect_structure()

    # dacă nu avem semnal activ → setăm
    if active_signal is None:
        if signal == "LONG":
            active_signal = "LONG"
            confirmation_count = 0
        elif signal == "SHORT":
            active_signal = "SHORT"
            confirmation_count = 0
        return None

    # 🟢 LONG logic
    if active_signal == "LONG" and structure == "HH":
        confirmation_count += 1

        if confirmation_count >= CONFIRMATION_TICKS:
            active_signal = None
            confirmation_count = 0
            return "ENTER LONG"

    # 🔴 SHORT logic
    elif active_signal == "SHORT" and structure == "LL":
        confirmation_count += 1

        if confirmation_count >= CONFIRMATION_TICKS:
            active_signal = None
            confirmation_count = 0
            return "ENTER SHORT"

    return None