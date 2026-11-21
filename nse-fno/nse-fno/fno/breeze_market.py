"""Read-only ICICI Breeze cash-market quotes."""
from __future__ import annotations

import math
import os
import threading
from typing import Any, Optional


class MarketDataError(Exception):
    """A user-actionable problem with the external market-data feed."""


def _number(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _value(data: dict[str, Any], *names: str) -> Any:
    return next((data[name] for name in names if data.get(name) not in (None, "")), None)


def _first_quote(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise MarketDataError("ICICI Breeze returned an invalid quote response.")
    error = response.get("Error") or response.get("error")
    if error:
        if "checksum" in str(error).casefold():
            raise MarketDataError(
                "ICICI Breeze rejected the request checksum. Verify BREEZE_API_SECRET belongs to "
                "BREEZE_API_KEY from the same Breeze API app. The session token cannot fix a key/secret mismatch."
            )
        raise MarketDataError(f"ICICI Breeze quote request failed: {error}")
    rows = response.get("Success")
    if isinstance(rows, dict):
        return rows
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0]
    raise MarketDataError("ICICI Breeze returned no stock quote.")


def _auth_failure_hint(exc: Exception) -> str:
    reason = str(exc).casefold()
    if "expired" in reason:
        return "Breeze says the session token has expired. Generate a fresh API session token."
    if "session key" in reason or "invalid session" in reason:
        return "Breeze rejected the session token. Generate a fresh API session token."
    if "api key" in reason or "public key" in reason:
        return "Breeze rejected the API key. Check that it belongs to the configured API app."
    return f"Could not initialize a Breeze session ({type(exc).__name__}). Check the credentials and server connectivity."


class BreezeMarketClient:
    def __init__(self, api_key: str, api_secret: str, session_token: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session_token = session_token
        self._client: Any = None
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, session_token: Optional[str] = None) -> "BreezeMarketClient":
        api_key = os.environ.get("BREEZE_API_KEY", "").strip()
        api_secret = os.environ.get("BREEZE_API_SECRET", "").strip()
        token = (session_token if session_token is not None else os.environ.get("BREEZE_SESSION_TOKEN", "")).strip()
        values = (("BREEZE_API_KEY", api_key), ("BREEZE_API_SECRET", api_secret), ("BREEZE_SESSION_TOKEN", token))
        missing = [name for name, value in values if not value]
        if missing:
            raise MarketDataError("Configure " + ", ".join(missing) + " in the server environment to use ICICI Breeze.")
        return cls(api_key, api_secret, token)

    def _connect(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from breeze_connect import BreezeConnect
        except ImportError as exc:
            raise MarketDataError("The ICICI Breeze SDK is missing. Install it with `python -m pip install -r requirements-live.txt`.") from exc
        try:
            client = BreezeConnect(api_key=self.api_key)
            client.generate_session(api_secret=self.api_secret, session_token=self.session_token)
        except Exception as exc:
            raise MarketDataError("ICICI Breeze authentication failed. " + _auth_failure_hint(exc)) from exc
        self._client = client
        return client

    def get_stock_quote(self, symbol: str) -> dict[str, Any]:
        with self._lock:
            client = self._connect()
            try:
                mapping = client.get_names(exchange_code="NSE", stock_code=symbol)
                stock_code = mapping.get("isec_stock_code") if isinstance(mapping, dict) else None
                if not stock_code:
                    raise MarketDataError(f"ICICI Breeze could not map NSE symbol {symbol}.")
                response = client.get_quotes(
                    stock_code=stock_code,
                    exchange_code="NSE",
                    product_type="cash",
                    expiry_date="",
                    right="",
                    strike_price="",
                )
            except MarketDataError:
                raise
            except Exception as exc:
                raise MarketDataError(f"ICICI Breeze quote request failed ({type(exc).__name__}); check the symbol, session, and API availability.") from exc

        quote = _first_quote(response)
        ltp = _number(quote.get("ltp"))
        if ltp is None or ltp <= 0:
            raise MarketDataError(f"ICICI Breeze did not return a valid live price for {symbol}.")
        return {
            "provider": "ICICI Breeze",
            "symbol": symbol,
            "ltp": ltp,
            "open": _number(quote.get("open")),
            "high": _number(quote.get("high")),
            "low": _number(quote.get("low")),
            "previous_close": _number(quote.get("previous_close")),
            "change": _number(quote.get("change")),
            "change_pct": _number(_value(quote, "change_percent", "change_percentage", "ltp_percent")),
            "volume": _number(_value(quote, "total_quantity_traded", "volume")),
            "timestamp": _value(quote, "ltt", "last_traded_time"),
        }
