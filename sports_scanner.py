"""
Sports probability scraper.
Tries multiple sources (scores24.live, sofascore API) to get real-world
win probabilities for a given match, then compares to Bayse prices.
"""
import re
import asyncio
import aiohttp
from urllib.parse import quote


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Mobile Safari/537.36"
    )
}

# SofaScore public API — no key needed, but rate-limit gently
SOFASCORE_SEARCH = "https://api.sofascore.com/api/v1/search/events?q={query}"
SOFASCORE_ODDS   = "https://api.sofascore.com/api/v1/event/{event_id}/odds/1/all"


async def _fetch(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                return await r.text()
    except Exception:
        pass
    return None


async def _fetch_json(session: aiohttp.ClientSession, url: str) -> dict | None:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                return await r.json()
    except Exception:
        pass
    return None


def _odds_to_prob(odds: float) -> float:
    """Convert decimal odds to implied probability."""
    if odds <= 0:
        return 0.0
    return round(1.0 / odds, 4)


async def get_sofascore_probability(team1: str, team2: str) -> dict | None:
    """
    Search SofaScore for the match and return:
      {"home_prob": 0.65, "draw_prob": 0.20, "away_prob": 0.15, "source": "sofascore"}
    Returns None if match not found or odds unavailable.
    """
    query = f"{team1} {team2}"
    url = SOFASCORE_SEARCH.format(query=quote(query))

    async with aiohttp.ClientSession() as session:
        data = await _fetch_json(session, url)
        if not data:
            return None

        events = data.get("events", [])
        if not events:
            return None

        # Pick the first live or upcoming event
        event_id = None
        for ev in events:
            status = ev.get("status", {}).get("type", "")
            if status in ("inprogress", "notstarted"):
                event_id = ev.get("id")
                break

        if not event_id:
            event_id = events[0].get("id")

        if not event_id:
            return None

        odds_data = await _fetch_json(session, SOFASCORE_ODDS.format(event_id=event_id))
        if not odds_data:
            return None

        # Parse 1X2 market
        for market in odds_data.get("markets", []):
            if market.get("marketName") in ("Full time", "1X2"):
                choices = {c["choiceName"]: c.get("fractionalValue") or c.get("decimalValue")
                           for c in market.get("choices", [])}
                try:
                    h = float(choices.get("1", 0) or choices.get("Home", 0))
                    d = float(choices.get("X", 0) or choices.get("Draw", 0))
                    a = float(choices.get("2", 0) or choices.get("Away", 0))
                    if h and a:
                        hp = _odds_to_prob(h)
                        dp = _odds_to_prob(d) if d else 0.0
                        ap = _odds_to_prob(a)
                        # Normalise to remove bookmaker margin
                        total = hp + dp + ap
                        return {
                            "home_prob": round(hp / total, 4),
                            "draw_prob": round(dp / total, 4),
                            "away_prob": round(ap / total, 4),
                            "source": "sofascore",
                        }
                except (ValueError, TypeError):
                    pass

    return None


def _extract_teams_from_title(title: str):
    """
    Try to extract team names from a Bayse event title like:
      "Will Man City beat Arsenal?"
      "Nigeria vs Cameroon — who wins?"
      "Chelsea to win vs Tottenham?"
    Returns (team1, team2) or (None, None).
    """
    title = title.lower()

    # Pattern: "X vs Y" or "X v Y"
    m = re.search(r"([a-z\s]+?)\s+(?:vs?\.?)\s+([a-z\s]+)", title)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # Pattern: "will X beat Y"
    m = re.search(r"will\s+(.+?)\s+beat\s+(.+?)[\?\s]", title)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # Pattern: "X to win"
    m = re.search(r"^(.+?)\s+to\s+win", title)
    if m:
        return m.group(1).strip(), None

    return None, None


async def get_real_probability(event_title: str, outcome_label: str) -> float | None:
    """
    Given a Bayse event title and an outcome label (YES/NO or team name),
    return the estimated real-world probability (0.0–1.0), or None if unavailable.

    For binary YES/NO markets, YES is interpreted as the home/first team winning.
    """
    team1, team2 = _extract_teams_from_title(event_title)
    if not team1:
        return None

    probs = await get_sofascore_probability(team1, team2 or "")
    if not probs:
        return None

    label = outcome_label.upper()

    if label == "YES":
        # YES = home/first team wins
        return probs["home_prob"]
    elif label == "NO":
        # NO = away team wins (or draw+away combined for simpler markets)
        return probs["away_prob"] + probs["draw_prob"]
    else:
        # Label might be a team name
        if team1 and team1 in label.lower():
            return probs["home_prob"]
        elif team2 and team2 in label.lower():
            return probs["away_prob"]

    return None


# ── Opportunity detector ───────────────────────────────────────────────────────

async def find_sports_opportunity(event: dict, min_gap: float = 0.10) -> dict | None:
    """
    Given a Bayse event dict, check if there's a mispricing opportunity.
    Returns a trade signal or None.

    Signal shape:
    {
        "event_id": str,
        "market_id": str,
        "outcome_id": str,
        "side": "BUY",
        "outcome_label": "YES" | "NO",
        "bayse_price": float,
        "real_prob": float,
        "gap": float,
        "title": str,
    }
    """
    title = event.get("title", "")
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

    # Get real probability for YES outcome
    real_yes = await get_real_probability(title, "YES")
    if real_yes is None:
        return None

    real_no = 1.0 - real_yes
    gap_yes = real_yes - yes_price  # positive = YES underpriced on Bayse
    gap_no  = real_no  - no_price   # positive = NO  underpriced on Bayse

    best_gap   = max(gap_yes, gap_no)
    best_label = "YES" if gap_yes >= gap_no else "NO"
    best_price = yes_price if best_label == "YES" else no_price
    best_real  = real_yes  if best_label == "YES" else real_no
    best_id    = yes_id    if best_label == "YES" else no_id

    if best_gap < min_gap:
        return None

    return {
        "event_id":      event["id"],
        "market_id":     market["id"],
        "outcome_id":    best_id,
        "side":          "BUY",
        "outcome_label": best_label,
        "bayse_price":   best_price,
        "real_prob":     best_real,
        "gap":           best_gap,
        "title":         title,
        "category":      "sports",
    }
