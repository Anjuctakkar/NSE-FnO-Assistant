import json
from unittest.mock import patch
from urllib.error import HTTPError

from fno import chat


class Response:
    def __init__(self, value):
        self.data = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size):
        return self.data[:size]


def request(provider):
    return {
        "provider": provider,
        "model": chat.DEFAULT_MODELS[provider],
        "api_key": "test-key",
        "messages": [{"role": "user", "content": "Explain the current values."}],
        "context": "TCS LTP ₹100; source Breeze live.",
    }


def test_provider_requests_are_formatted_and_api_key_is_not_in_url():
    cases = {
        "openai": ({"choices": [{"message": {"content": "OpenAI answer"}}]}, "OpenAI answer", "https://api.openai.com/v1/chat/completions"),
        "anthropic": ({"content": [{"type": "text", "text": "Anthropic answer"}]}, "Anthropic answer", "https://api.anthropic.com/v1/messages"),
        "gemini": ({"candidates": [{"content": {"parts": [{"text": "Gemini answer"}]}}]}, "Gemini answer",
                   "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"),
    }
    key_headers = {"openai": "Authorization", "anthropic": "X-api-key", "gemini": "X-goog-api-key"}
    for provider, (response, expected_text, expected_url) in cases.items():
        with patch("fno.chat.urlopen", return_value=Response(response)) as send:
            result = chat.run(request(provider))
        outbound = send.call_args.args[0]
        body = json.loads(outbound.data)
        assert result == {"text": expected_text, "provider": provider,
                          "model": chat.DEFAULT_MODELS[provider]}
        assert outbound.full_url == expected_url and "test-key" not in outbound.full_url
        assert outbound.get_header(key_headers[provider]) in ("Bearer test-key", "test-key")
        serialized = json.dumps(body)
        assert "TCS LTP" in serialized and "Explain the current values." in serialized
        if provider == "openai":
            assert body["messages"][0]["role"] == "system"
        elif provider == "anthropic":
            assert body["system"].startswith(chat.SYSTEM_PROMPT)
        else:
            assert body["systemInstruction"]["parts"][0]["text"].startswith(chat.SYSTEM_PROMPT)


def test_chat_rejects_unknown_provider_missing_key_and_invalid_history():
    for payload, expected in (
        ({"provider": "not-a-provider"}, "Choose OpenAI"),
        ({"provider": "openai", "api_key": ""}, "Enter a valid API key"),
        ({**request("openai"), "messages": [{"role": "system", "content": "no"}]}, "user or assistant role"),
        ({**request("openai"), "messages": [{"role": "assistant", "content": "not a question"}]}, "does not end with a user question"),
    ):
        try:
            chat.run(payload)
        except chat.ChatError as exc:
            assert expected in str(exc) and not exc.upstream
        else:
            raise AssertionError("invalid chat request was accepted")


def test_provider_auth_error_is_safe_and_does_not_echo_provider_body():
    error = HTTPError("https://api.openai.com/v1/chat/completions", 401, "unauthorized", None,
                      Response({"error": {"message": "test-key was rejected"}}))
    with patch("fno.chat.urlopen", side_effect=error):
        try:
            chat.run(request("openai"))
        except chat.ChatError as exc:
            assert exc.upstream and "test-key" not in str(exc)
            assert "API key" in str(exc)
        else:
            raise AssertionError("provider authentication error was swallowed")
