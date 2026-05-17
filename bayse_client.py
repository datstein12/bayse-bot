"""
Bayse Markets API client with HMAC-SHA256 request signing.
"""
import time
import hmac
import hashlib
import base64
import json
import aiohttp

BASE_URL = "https://relay.bayse.markets/v1"


class BayseClient:
    def __init__(self, public_key: str, secret_key: str, currency: str = "NGN"):
        self.public_key = public_key
        self.secret_key = secret_key
        self.currency = currency

    # ── Signing ───────────────────────────────────────────────────────────────
    def _sign(self, method: str, path: str, body: str = "") -> dict:
        timestamp = str(int(time.time()))
        body_hash = hashlib.sha256(body.encode()).hexdigest()
        payload = f"{timestamp}.{method}.{path}.{body_hash}"
        sig = base64.b64encode(
            hmac.new(self.secret_key.encode(), payload.encode(), hashlib.sha256).digest()
        ).decode()
        return {
            "X-Public-Key": self.public_key,
            "X-Timestamp": timestamp,
            "X-Signature": sig,
            "Content-Type": "application/json",
        }

    def _read_headers(self) -> dict:
        return {"X-Public-Key": self.public_key}

    # ── HTTP helpers ──────────────────────────────────────────────────────────
    async def get(self, path: str, params: dict = None) -> dict:
        async with aiohttp.ClientSession() as s:
            async with s.get(BASE_URL + path, headers=self._read_headers(), params=params) as r:
                return await r.json()

    async def post(self, path: str, body: dict) -> dict:
        body_str = json.dumps(body)
        headers = self._sign("POST", path, body_str)
        async with aiohttp.ClientSession() as s:
            async with s.post(BASE_URL + path, headers=headers, data=body_str) as r:
                return await r.json()

    async def delete(self, path: str) -> dict:
        headers = self._sign("DELETE", path, "")
        async with aiohttp.ClientSession() as s:
            async with s.delete(BASE_URL + path, headers=headers) as r:
                return await r.json()

    # ── Market data ───────────────────────────────────────────────────────────
    async def list_events(self, category: str = None, page: int = 1, size: int = 50) -> dict:
        params = {"status": "open", "size": str(size), "page": str(page),
                  "currency": self.currency}
        if category:
            params["category"] = category
        return await self.get("/pm/events", params)

    async def get_event(self, event_id: str) -> dict:
        return await self.get(f"/pm/events/{event_id}")

    async def get_ticker(self, market_id: str) -> dict:
        return await self.get(f"/pm/markets/{market_id}/ticker")

    async def get_price_history(self, event_id: str) -> dict:
        return await self.get(f"/pm/events/{event_id}/price-history")

    async def get_wallet(self) -> dict:
        return await self.get("/wallet/assets")

    async def get_portfolio(self) -> dict:
        return await self.get("/pm/portfolio")

    async def get_pnl(self) -> dict:
        return await self.get("/pm/pnl")

    async def list_orders(self) -> dict:
        return await self.get("/pm/orders")

    async def cancel_order(self, order_id: str) -> dict:
        return await self.delete(f"/pm/orders/{order_id}")

    # ── Trading ───────────────────────────────────────────────────────────────
    async def get_quote(self, event_id: str, market_id: str, side: str,
                        outcome_id: str, amount: float) -> dict:
        path = f"/pm/events/{event_id}/markets/{market_id}/quote"
        return await self.post(path, {
            "side": side,
            "outcomeId": outcome_id,
            "amount": amount,
            "type": "MARKET",
            "currency": self.currency,
        })

    async def place_order(self, event_id: str, market_id: str, side: str,
                          outcome_id: str, amount: float) -> dict:
        path = f"/pm/events/{event_id}/markets/{market_id}/orders"
        return await self.post(path, {
            "side": side,
            "outcomeId": outcome_id,
            "amount": amount,
            "type": "MARKET",
            "currency": self.currency,
        })

    # ── Wallet helpers ────────────────────────────────────────────────────────
    async def available_balance(self) -> float:
        """Return available NGN (or USD) balance."""
        data = await self.get_wallet()
        for asset in data.get("assets", []):
            if asset.get("currency") == self.currency:
                return float(asset.get("available", 0))
        return 0.0
