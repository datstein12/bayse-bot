"""
Crypto scanner — uses Bayse price history + ticker to detect momentum
and find YES/NO mispricing on crypto prediction markets.
"""


def _price_trend(prices: list[float], window: int = 5) -> str:
    """
    Simple momentum: compare average of last `window` prices to
    average of the window before that.
    Returns "UP", "DOWN", or "FLAT".
    """
    if len(prices) < window * 2:
        return "FLAT"
    recent = sum(prices[-window:]) / window
    previous = sum(prices[-window * 2:-window]) / window
    change = (recent - previous) / previous if previous else 0
    if change > 0.01:
        return "UP"
    elif change < -0.01:
        return "DOWN"
    return "FLAT"


def _extract_crypto_signal(event: dict, history_data: dict) -> dict | None:
    """
    For a crypto Bayse event (e.g. "Will BTC close above $70k?"),
    use price history of the YES outcome to determine momentum.

    Returns a trade signal or None.
    """
    markets = event.get("markets", [])
    if not markets:
        return None

    market = markets[0]
    if market.get("status") != "open":
        return None

    yes_price = float(market.get("yesBuyPrice") or market.get("outcome1Price") or 0)
    no_price  = float(market.get("noBuyPrice")  or market.get("outcome2Price") or 0)
    yes_id    = market.get("outcome1Id")
    no_id     = market.get("outcome2Id")

    if not yes_price or not yes_id:
        return None

    # Extract price history for YES
    history = history_data.get("history", [])
    yes_prices = []
    for entry in history:
        p = entry.get("outcome1Price") or entry.get("yesPrice")
        if p is not None:
            yes_prices.append(float(p))

    if len(yes_prices) < 4:
        return None

    trend = _price_trend(yes_prices)
    if trend == "FLAT":
        return None

    # Trend UP → market expects YES more likely → buy YES
    # Trend DOWN → market expects NO more likely → buy NO
    if trend == "UP":
        outcome_label = "YES"
        outcome_id    = yes_id
        bayse_price   = yes_price
    else:
        outcome_label = "NO"
        outcome_id    = no_id
        bayse_price   = no_price

    # Only trade if price is not already saturated (between 0.15 and 0.85)
    if not (0.15 <= bayse_price <= 0.85):
        return None

    return {
        "event_id":      event["id"],
        "market_id":     market["id"],
        "outcome_id":    outcome_id,
        "side":          "BUY",
        "outcome_label": outcome_label,
        "bayse_price":   bayse_price,
        "trend":         trend,
        "title":         event.get("title", ""),
        "category":      "crypto",
    }


async def find_crypto_opportunity(event: dict, client) -> dict | None:
    """
    Fetch price history for a crypto event and return a signal if trend detected.
    """
    try:
        history = await client.get_price_history(event["id"])
        signal = _extract_crypto_signal(event, history)
        return signal
    except Exception:
        return None
