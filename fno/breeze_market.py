"""Read-only ICICI Breeze cash quotes, option chains, and historical candles."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional


class MarketDataError(Exception):
    """A user-actionable problem with the external market-data feed."""


_IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


class RateLimiter:
    """Breeze allows about 100 calls a minute and 5,000 a day per user (an older ICICI FAQ says 75 a minute: the default here is
    the safer 75; override with BREEZE_CALLS_PER_MIN / BREEZE_CALLS_PER_DAY). A whole-market scan makes hundreds of calls, so every
    call goes through this: it waits when the minute window is full, counts the day's calls (kept in a small file when one is
    configured, so a restart does not forget them) and refuses once the day's budget is spent. Time is injectable for tests."""

    def __init__(self, per_minute: int = 75, per_day: int = 5000, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, today: Optional[Callable[[], str]] = None,
                 state_path: Optional[Path] = None):
        self.per_minute, self.per_day = max(1, int(per_minute)), max(1, int(per_day))
        self._clock, self._sleep = clock, sleep
        self._today = today or (lambda: dt.datetime.now(_IST).date().isoformat())
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()
        self._day, self._used = self._today(), 0
        self.state_path = Path(state_path) if state_path else None
        self._load()

    @classmethod
    def from_env(cls) -> "RateLimiter":
        def num(name: str, default: int) -> int:
            try:
                return max(1, int(os.environ.get(name, default)))
            except ValueError:
                return default
        return cls(num("BREEZE_CALLS_PER_MIN", 75), num("BREEZE_CALLS_PER_DAY", 5000))

    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            if d.get("day") == self._day:
                self._used = int(d.get("used", 0))
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({"day": self._day, "used": self._used}), encoding="utf-8")
        except OSError:
            pass

    def use_state_file(self, path: Path) -> None:
        with self._lock:
            self.state_path = Path(path)
            self._load()

    def _roll(self) -> None:
        today = self._today()
        if today != self._day:
            self._day, self._used = today, 0

    @property
    def used_today(self) -> int:
        with self._lock:
            self._roll()
            return self._used

    @property
    def remaining_today(self) -> int:
        return max(0, self.per_day - self.used_today)

    def acquire(self) -> None:
        """Block until a call may be made, then count it. Raises MarketDataError when the day's budget is spent."""
        while True:
            with self._lock:
                self._roll()
                if self._used >= self.per_day:
                    raise MarketDataError(f"The ICICI Breeze daily limit of {self.per_day} calls is used up. It resets tomorrow.")
                now = self._clock()
                while self._stamps and now - self._stamps[0] >= 60.0:
                    self._stamps.popleft()
                if len(self._stamps) < self.per_minute:
                    self._stamps.append(now)
                    self._used += 1
                    self._save()
                    return
                wait = 60.0 - (now - self._stamps[0]) + 0.05
            self._sleep(max(wait, 0.05))


GLOBAL_LIMITER = RateLimiter.from_env()


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


def _response_rows(response: Any, operation: str, allow_empty: bool = False) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        raise MarketDataError(f"ICICI Breeze returned an invalid {operation} response.")
    error = response.get("Error") or response.get("error")
    if error:
        if "checksum" in str(error).casefold():
            _first_quote({"Error": error})
        raise MarketDataError(f"ICICI Breeze {operation} failed: {error}")
    rows = response.get("Success")
    if isinstance(rows, dict):
        return [rows]
    if isinstance(rows, list):
        result = [row for row in rows if isinstance(row, dict)]
        if result or allow_empty:
            return result
    elif allow_empty and rows == []:
        return []
    raise MarketDataError(f"ICICI Breeze returned no {operation} data.")


def _request_failure(operation: str, exc: Exception) -> MarketDataError:
    if "checksum" in str(exc).casefold():
        return MarketDataError(
            "ICICI Breeze rejected the request checksum. Verify the API secret matches the API key from the same Breeze app."
        )
    return MarketDataError(f"ICICI Breeze {operation} request failed ({type(exc).__name__}).")


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
        self._stock_codes: dict[str, str] = {}
        self.limiter: Optional[RateLimiter] = None        # None = the process-wide limiter

    def _throttle(self) -> None:
        (self.limiter or GLOBAL_LIMITER).acquire()

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

    def test_connection(self) -> None:
        client = self._connect()
        self._throttle()
        try:
            response = client.get_quotes(
                stock_code="TCS",
                exchange_code="NSE",
                product_type="cash",
                expiry_date="",
                right="",
                strike_price="",
            )
        except Exception as exc:
            raise MarketDataError(
                f"ICICI Breeze connection test failed ({type(exc).__name__}). Check the credentials and server connectivity."
            ) from exc
        _first_quote(response)

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

    def _nse_stock_code(self, client: Any, symbol: str) -> str:
        if symbol in self._stock_codes:
            return self._stock_codes[symbol]
        self._throttle()
        try:
            mapping = client.get_names(exchange_code="NSE", stock_code=symbol)
        except Exception as exc:
            raise MarketDataError(
                f"ICICI Breeze could not resolve NSE symbol {symbol} ({type(exc).__name__})."
            ) from exc
        stock_code = mapping.get("isec_stock_code") if isinstance(mapping, dict) else None
        if not stock_code:
            raise MarketDataError(f"ICICI Breeze could not map NSE symbol {symbol}.")
        self._stock_codes[symbol] = stock_code
        return stock_code

    def get_stock_quote(self, symbol: str) -> dict[str, Any]:
        with self._lock:
            client = self._connect()
            try:
                stock_code = self._nse_stock_code(client, symbol)
                self._throttle()
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

    def get_option_chain_records(self, symbol: str, expiry: str) -> list[dict[str, Any]]:
        client = self._connect()
        stock_code = self._nse_stock_code(client, symbol)
        records = []
        for right in ("call", "put"):
            self._throttle()
            try:
                response = client.get_option_chain_quotes(
                    stock_code=stock_code,
                    exchange_code="NFO",
                    expiry_date=expiry,
                    product_type="options",
                    right=right,
                    strike_price="",
                )
            except Exception as exc:
                raise _request_failure("option-chain", exc) from exc
            rows = _response_rows(response, "option-chain")
            records.extend({**row, "right": row.get("right") or right} for row in rows)
        return records

    def get_intraday_bars(self, symbol: str, from_date: str, to_date: str,
                          interval: str = "5minute") -> list[dict[str, Any]]:
        client = self._connect()
        stock_code = self._nse_stock_code(client, symbol)
        self._throttle()
        try:
            response = client.get_historical_data_v2(
                interval=interval,
                from_date=from_date,
                to_date=to_date,
                stock_code=stock_code,
                exchange_code="NSE",
                product_type="cash",
            )
        except Exception as exc:
            raise _request_failure("intraday history", exc) from exc
        return _response_rows(response, "intraday history", allow_empty=True)

    def get_option_history(self, symbol: str, expiry: str, strike: float, right: str,
                           from_date: str, to_date: str,
                           interval: str = "5minute") -> list[dict[str, Any]]:
        client = self._connect()
        stock_code = self._nse_stock_code(client, symbol)
        self._throttle()
        try:
            response = client.get_historical_data_v2(
                interval=interval,
                from_date=from_date,
                to_date=to_date,
                stock_code=stock_code,
                exchange_code="NFO",
                product_type="options",
                expiry_date=expiry,
                right=right,
                strike_price=str(strike),
            )
        except Exception as exc:
            raise _request_failure("option history", exc) from exc
        return _response_rows(response, "option history", allow_empty=True)

    def get_daily_bars(self, symbol: str, from_date: str, to_date: str) -> list[dict[str, Any]]:
        """Daily cash candles (real open/high/low/close): the whole-market scan's trend and ATR come from these, not from 5-minute
        candles, because the trades it suggests are held for weeks."""
        return self.get_intraday_bars(symbol, from_date, to_date, "1day")
