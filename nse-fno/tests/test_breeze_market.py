import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from fno import service
from fno.breeze_market import BreezeMarketClient, MarketDataError


class FakeBreeze:
    instances = []

    def __init__(self, api_key):
        self.api_key = api_key
        self.calls = []
        self.__class__.instances.append(self)

    def generate_session(self, api_secret, session_token):
        self.credentials = (api_secret, session_token)

    def get_names(self, exchange_code, stock_code):
        self.calls.append(("names", exchange_code, stock_code))
        return {"isec_stock_code": "TCS"}

    def get_quotes(self, **kwargs):
        self.calls.append(("quotes", kwargs))
        return {"Success": [{
            "ltp": "2406.60", "open": "2395", "high": "2448", "low": "2384",
            "previous_close": "2405", "change": "1.60", "ltp_percent": "0.07",
            "total_quantity_traded": "3027101", "last_traded_time": "2026-11-17 16:00:29",
        }]}

    def get_option_chain_quotes(self, **kwargs):
        self.calls.append(("option_chain", kwargs))
        return {"Success": [{"strike_price": "100", "ltp": "5.5", "open_interest": "1200"}]}

    def get_historical_data_v2(self, **kwargs):
        self.calls.append(("history", kwargs))
        return {"Success": [{"datetime": "2026-11-17T09:15:00.000Z", "open": "100", "high": "101",
                             "low": "99", "close": "100.5"}]}


def install_fake_sdk():
    FakeBreeze.instances = []
    return patch.dict(sys.modules, {"breeze_connect": SimpleNamespace(BreezeConnect=FakeBreeze)})


def test_breeze_quote_normalizes_response_and_authenticates_with_environment_credentials():
    env = {"BREEZE_API_KEY": "key", "BREEZE_API_SECRET": "secret", "BREEZE_SESSION_TOKEN": "session"}
    with patch.dict(os.environ, env, clear=True), install_fake_sdk():
        quote = BreezeMarketClient.from_env().get_stock_quote("TCS")
    client = FakeBreeze.instances[0]
    assert client.api_key == "key" and client.credentials == ("secret", "session")
    assert client.calls[0] == ("names", "NSE", "TCS")
    assert client.calls[1] == ("quotes", {
        "stock_code": "TCS", "exchange_code": "NSE", "product_type": "cash",
        "expiry_date": "", "right": "", "strike_price": "",
    })
    assert quote["provider"] == "ICICI Breeze" and quote["symbol"] == "TCS"
    assert quote["ltp"] == 2406.6 and quote["change_pct"] == 0.07
    assert quote["timestamp"] == "2026-11-17 16:00:29"


def test_breeze_live_market_methods_forward_expiry_interval_and_option_identity():
    with install_fake_sdk():
        client = BreezeMarketClient("key", "secret", "session")
        chain = client.get_option_chain_records("TCS", "2026-12-29T00:00:00.000Z")
        bars = client.get_intraday_bars("TCS", "2026-11-10T09:15:00.000Z", "2026-11-17T15:00:00.000Z")
        options = client.get_option_history("TCS", "2026-12-29T00:00:00.000Z", 100, "call",
                                            "2026-11-10T09:15:00.000Z", "2026-11-17T15:00:00.000Z")
    calls = FakeBreeze.instances[0].calls
    assert [row["right"] for row in chain] == ["call", "put"]
    assert bars[0]["close"] == "100.5" and options[0]["datetime"].startswith("2026-11-17")
    assert [c[1]["right"] for c in calls if c[0] == "option_chain"] == ["call", "put"]
    history = [c[1] for c in calls if c[0] == "history"]
    assert history[0]["interval"] == "5minute" and history[0]["exchange_code"] == "NSE"
    assert history[1]["exchange_code"] == "NFO" and history[1]["strike_price"] == "100"


def test_breeze_quote_uses_a_session_token_entered_in_the_ui():
    env = {"BREEZE_API_KEY": "key", "BREEZE_API_SECRET": "secret", "BREEZE_SESSION_TOKEN": "old-session"}
    with patch.dict(os.environ, env, clear=True), install_fake_sdk():
        quote = BreezeMarketClient.from_env(session_token="entered-session").get_stock_quote("TCS")
    assert FakeBreeze.instances[0].credentials == ("secret", "entered-session")
    assert quote["ltp"] == 2406.6


def test_breeze_connection_test_verifies_a_signed_read_only_quote():
    with install_fake_sdk():
        client = BreezeMarketClient("key", "secret", "session")
        client.test_connection()
    sdk_client = FakeBreeze.instances[0]
    assert sdk_client.credentials == ("secret", "session")
    assert sdk_client.calls == [("quotes", {
        "stock_code": "TCS", "exchange_code": "NSE", "product_type": "cash",
        "expiry_date": "", "right": "", "strike_price": "",
    })]


def test_breeze_connection_test_rejects_an_invalid_request_checksum():
    class InvalidChecksumBreeze(FakeBreeze):
        def get_quotes(self, **kwargs):
            return {"Error": "Authentication Fail :: Invalid Checksum."}

    with patch.dict(sys.modules, {"breeze_connect": SimpleNamespace(BreezeConnect=InvalidChecksumBreeze)}):
        try:
            BreezeMarketClient("key", "wrong-secret", "session").test_connection()
        except MarketDataError as exc:
            assert "BREEZE_API_SECRET belongs to BREEZE_API_KEY" in str(exc)
        else:
            raise AssertionError("the connection test must reject bad request checksums")


def test_breeze_quote_requires_all_credentials_and_rejects_bad_prices():
    with patch.dict(os.environ, {}, clear=True):
        try:
            BreezeMarketClient.from_env()
        except MarketDataError as exc:
            assert "BREEZE_API_KEY" in str(exc) and "BREEZE_SESSION_TOKEN" in str(exc)
        else:
            raise AssertionError("missing Breeze credentials should be reported")

    class BadPriceBreeze(FakeBreeze):
        def get_quotes(self, **kwargs):
            return {"Success": [{"ltp": "invalid"}]}

    sdk = SimpleNamespace(BreezeConnect=BadPriceBreeze)
    env = {"BREEZE_API_KEY": "key", "BREEZE_API_SECRET": "secret", "BREEZE_SESSION_TOKEN": "session"}
    with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, {"breeze_connect": sdk}):
        try:
            BreezeMarketClient.from_env().get_stock_quote("TCS")
        except MarketDataError as exc:
            assert "valid live price" in str(exc)
        else:
            raise AssertionError("invalid Breeze prices should be rejected")


def test_breeze_authentication_errors_explain_rejected_credentials_without_echoing_sdk_text():
    class RejectingBreeze(FakeBreeze):
        def generate_session(self, api_secret, session_token):
            raise Exception("Unexpected error: Could not authenticate credentials. Please check session key.")

    env = {"BREEZE_API_KEY": "key", "BREEZE_API_SECRET": "secret", "BREEZE_SESSION_TOKEN": "session"}
    with patch.dict(os.environ, env, clear=True), patch.dict(
        sys.modules, {"breeze_connect": SimpleNamespace(BreezeConnect=RejectingBreeze)}
    ):
        try:
            BreezeMarketClient.from_env().get_stock_quote("TCS")
        except MarketDataError as exc:
            assert "Breeze rejected the session token" in str(exc)
            assert "Unexpected error" not in str(exc)
        else:
            raise AssertionError("rejected session tokens should be reported")


def test_breeze_checksum_error_points_to_the_api_key_secret_pair():
    with patch.dict(os.environ, {"BREEZE_API_KEY": "key", "BREEZE_API_SECRET": "secret", "BREEZE_SESSION_TOKEN": "session"}, clear=True):
        class InvalidChecksumBreeze(FakeBreeze):
            def get_quotes(self, **kwargs):
                return {"Error": "Authentication Fail :: Invalid Checksum."}

        with patch.dict(sys.modules, {"breeze_connect": SimpleNamespace(BreezeConnect=InvalidChecksumBreeze)}):
            try:
                BreezeMarketClient.from_env().get_stock_quote("TCS")
            except MarketDataError as exc:
                assert "BREEZE_API_SECRET belongs to BREEZE_API_KEY" in str(exc)
                assert "session token cannot fix" in str(exc)
            else:
                raise AssertionError("invalid Breeze checksums should be explained")


def test_service_validates_symbol_and_surfaces_breeze_errors():
    ctx = service.Context(tempfile.mkdtemp())
    client = SimpleNamespace(api_key="", api_secret="", session_token="", get_stock_quote=lambda symbol: {"symbol": symbol, "ltp": 2406.6})
    with patch.object(BreezeMarketClient, "from_env", return_value=client) as create:
        assert service.live_quote(ctx, {"symbol": "TCS"})["ltp"] == 2406.6
        create.assert_called_once()
        try:
            service.live_quote(ctx, {"symbol": "../invalid"})
        except service.ServiceError:
            pass
        else:
            raise AssertionError("invalid symbols must be rejected")
        client.get_stock_quote = lambda symbol: (_ for _ in ()).throw(MarketDataError("Breeze is unavailable"))
        try:
            service.live_quote(ctx, {"symbol": "TCS"})
        except service.UpstreamServiceError as exc:
            assert "Breeze is unavailable" in str(exc)
        else:
            raise AssertionError("Breeze failures should be surfaced")


def test_service_passes_a_supplied_session_token_to_the_breeze_client():
    ctx = service.Context(tempfile.mkdtemp())
    client = SimpleNamespace(api_key="key", api_secret="secret", session_token="typed-token",
                             get_stock_quote=lambda symbol: {"symbol": symbol, "ltp": 2406.6})
    with patch.object(BreezeMarketClient, "from_env", return_value=client) as create:
        quote = service.live_quote(ctx, {"symbol": "TCS", "session_token": " typed-token "})
    assert quote["ltp"] == 2406.6
    create.assert_called_once_with(session_token="typed-token")


def test_service_connection_test_uses_supplied_credentials_and_requires_all_fields():
    with install_fake_sdk():
        result = service.test_breeze_connection({"api_key": " key ", "api_secret": " secret ", "session_token": " session "})
    assert result == {"ok": True, "message": "Connected to ICICI Breeze."}
    assert FakeBreeze.instances[0].api_key == "key"
    assert FakeBreeze.instances[0].credentials == ("secret", "session")
    try:
        service.test_breeze_connection({"api_key": "key", "api_secret": "", "session_token": "session"})
    except service.ServiceError as exc:
        assert "API secret" in str(exc)
    else:
        raise AssertionError("partial Breeze credentials must be rejected")


def test_service_connection_test_falls_back_to_complete_environment_credentials():
    env = {"BREEZE_API_KEY": "key", "BREEZE_API_SECRET": "secret", "BREEZE_SESSION_TOKEN": "session"}
    with patch.dict(os.environ, env, clear=True), install_fake_sdk():
        result = service.test_breeze_connection({"api_key": "", "api_secret": "", "session_token": ""})
    assert result["ok"] is True
    assert FakeBreeze.instances[0].credentials == ("secret", "session")


def test_live_quote_uses_credentials_entered_in_settings():
    ctx = service.Context(tempfile.mkdtemp())
    with install_fake_sdk():
        quote = service.live_quote(ctx, {
            "symbol": "TCS", "api_key": "key", "api_secret": "secret", "session_token": "session",
        })
    assert quote["ltp"] == 2406.6
    assert FakeBreeze.instances[0].api_key == "key"
    assert FakeBreeze.instances[0].credentials == ("secret", "session")
    assert ctx._breeze_market is None
