from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any


class BrokerError(RuntimeError): pass


def request_json(url: str, *, token: str = "", method: str = "GET", payload: dict | None = None,
                 headers: dict[str, str] | None = None) -> dict:
    values = {"Accept": "application/json", "User-Agent": "primus-multi-asset/1.0", **(headers or {})}
    if token: values["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode() if payload is not None else None
    if data: values["Content-Type"] = "application/json"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=values, method=method), timeout=20) as response:
            value = json.loads(response.read().decode())
            return value if isinstance(value, dict) else {"items": value}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise BrokerError(f"broker HTTP {exc.code}: {detail}") from exc


@dataclass(frozen=True)
class BrokerPolicy:
    live_enabled: bool = False
    max_order_risk_usd: float = 25.0

    @classmethod
    def from_env(cls) -> "BrokerPolicy":
        # Deliberately fail closed. A code deployment alone can never enable live orders.
        return cls(live_enabled=os.getenv("MULTI_ASSET_LIVE_ENABLED", "false").lower() == "true",
                   max_order_risk_usd=min(250.0, float(os.getenv("ASSET_MAX_RISK_USD", "25"))))


class OandaAdapter:
    def __init__(self):
        self.token = os.environ["OANDA_API_TOKEN"]
        self.account = os.environ["OANDA_ACCOUNT_ID"]
        env = os.getenv("OANDA_ENVIRONMENT", "practice").lower()
        self.base = "https://api-fxpractice.oanda.com" if env == "practice" else "https://api-fxtrade.oanda.com"
        self.environment = env

    def preflight(self) -> dict:
        payload = request_json(f"{self.base}/v3/accounts/{self.account}/summary", token=self.token)
        account = payload.get("account", {})
        return {"broker": "OANDA", "environment": self.environment, "account_id_suffix": self.account[-4:],
                "currency": account.get("currency"), "balance": float(account.get("balance", 0)),
                "nav": float(account.get("NAV", account.get("balance", 0))),
                "realized_pl": float(account.get("pl", 0)),
                "unrealized_pl": float(account.get("unrealizedPL", 0)),
                "financing": float(account.get("financing", 0)),
                "margin_available": float(account.get("marginAvailable", 0)),
                "margin_used": float(account.get("marginUsed", 0)),
                "open_trade_count": int(account.get("openTradeCount", 0)),
                "last_transaction_id": str(payload.get("lastTransactionID") or account.get("lastTransactionID") or "")}

    def instruments(self) -> list[dict[str, Any]]:
        return request_json(f"{self.base}/v3/accounts/{self.account}/instruments", token=self.token).get("instruments", [])

    def instrument(self, symbol: str) -> dict:
        query = urllib.parse.urlencode({"instruments": symbol})
        values = request_json(f"{self.base}/v3/accounts/{self.account}/instruments?{query}", token=self.token).get("instruments", [])
        if not values: raise BrokerError("OANDA returned no instrument metadata")
        return values[0]

    def candles(self, symbol: str, granularity: str = "H1", count: int = 30) -> list[dict]:
        query = urllib.parse.urlencode({"granularity": granularity, "count": count, "price": "M"})
        return request_json(f"{self.base}/v3/instruments/{symbol}/candles?{query}", token=self.token).get("candles", [])

    def price(self, symbol: str) -> dict:
        query = urllib.parse.urlencode({"instruments": symbol})
        values = request_json(f"{self.base}/v3/accounts/{self.account}/pricing?{query}", token=self.token).get("prices", [])
        if not values: raise BrokerError("OANDA returned no price")
        return values[0]

    def preview(self, proposal: dict) -> dict:
        units = int(round(float(proposal["quantity"]))) * (1 if proposal["side"] == "BUY" else -1)
        return {"broker": "OANDA", "environment": self.environment, "preview_only": True,
                "client_order_id": proposal.get("proposal_id") or str(uuid.uuid4()), "instrument": proposal["symbol"],
                "units": units, "stop_loss": proposal["stop_price"], "take_profit": proposal["target_price"],
                "maximum_loss_usd": proposal["maximum_loss_usd"]}

    def create_order(self, proposal: dict, *, client_order_id: str) -> dict:
        """Create one protected OANDA order. Callers must enforce live policy first."""
        units = int(round(float(proposal["quantity"]))) * (1 if proposal["side"] == "BUY" else -1)
        if units == 0:
            raise BrokerError("order units round to zero")
        reference = float(proposal["reference_price"])
        drift_bps = min(50.0, max(1.0, float(os.getenv("FOREX_MAX_ENTRY_DRIFT_BPS", "15"))))
        price_bound = reference * (1 + drift_bps / 10_000) if units > 0 else reference * (1 - drift_bps / 10_000)
        precision = int(self.instrument(proposal["symbol"]).get("displayPrecision") or 5)
        order = {
            "type": "MARKET",
            "instrument": proposal["symbol"],
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "priceBound": f"{price_bound:.{precision}f}",
            "clientExtensions": {"id": client_order_id, "tag": "primus-forex-v1"},
            "stopLossOnFill": {"price": f'{float(proposal["stop_price"]):.{precision}f}', "timeInForce": "GTC"},
            "takeProfitOnFill": {"price": f'{float(proposal["target_price"]):.{precision}f}', "timeInForce": "GTC"},
        }
        return request_json(
            f"{self.base}/v3/accounts/{self.account}/orders",
            token=self.token,
            method="POST",
            payload={"order": order},
        )

    def open_trades(self) -> list[dict[str, Any]]:
        return request_json(f"{self.base}/v3/accounts/{self.account}/openTrades", token=self.token).get("trades", [])

    def trade(self, trade_id: str) -> dict:
        return request_json(f"{self.base}/v3/accounts/{self.account}/trades/{trade_id}", token=self.token)

    def pending_orders(self) -> list[dict[str, Any]]:
        return request_json(f"{self.base}/v3/accounts/{self.account}/pendingOrders", token=self.token).get("orders", [])

    def transactions_since(self, transaction_id: str) -> dict:
        query = urllib.parse.urlencode({"id": transaction_id})
        return request_json(f"{self.base}/v3/accounts/{self.account}/transactions/sinceid?{query}", token=self.token)

    def close_trade(self, trade_id: str) -> dict:
        return request_json(
            f"{self.base}/v3/accounts/{self.account}/trades/{trade_id}/close",
            token=self.token,
            method="PUT",
            payload={"units": "ALL"},
        )


class AlpacaAdapter:
    def __init__(self):
        self.key = os.environ["ALPACA_API_KEY"]
        self.secret = os.environ["ALPACA_API_SECRET"]
        self.base = os.getenv("ALPACA_TRADING_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
        self.headers = {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}

    def preflight(self) -> dict:
        account = request_json(self.base + "/v2/account", headers=self.headers)
        return {"broker": "ALPACA", "paper": "paper-api" in self.base, "status": account.get("status"),
                "currency": account.get("currency"), "buying_power": float(account.get("buying_power", 0)),
                "options_buying_power": float(account.get("options_buying_power", 0) or 0)}

    def preview(self, proposal: dict) -> dict:
        if proposal["asset_class"] == "OPTION" and proposal["strategy"] not in {"CALL_DEBIT_SPREAD", "PUT_DEBIT_SPREAD"}:
            raise BrokerError("only defined-risk debit spreads are supported")
        return {"broker": "ALPACA", "paper": "paper-api" in self.base, "preview_only": True,
                "client_order_id": proposal.get("proposal_id") or str(uuid.uuid4()), "symbol": proposal["symbol"],
                "side": proposal["side"].lower(), "qty": proposal["quantity"], "type": "limit",
                "limit_price": proposal["reference_price"], "maximum_loss_usd": proposal["maximum_loss_usd"]}
