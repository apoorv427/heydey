"""Thin OpenRouter client — the 200-with-error-body crash class (found live:
a mid-run provider error surfaced as a bare KeyError and killed the B7
experiment). The client must fail closed with the provider's message."""

import io
import json

import pytest

from heydey import llm_client


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_returning(payload: dict):
    def fake_urlopen(req, timeout=0):
        return _FakeResponse(json.dumps(payload).encode())
    return fake_urlopen


@pytest.fixture()
def _key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")


def test_error_body_raises_llmerror_with_provider_message(_key, monkeypatch):
    monkeypatch.setattr(llm_client.urllib.request, "urlopen", _urlopen_returning(
        {"error": {"message": "Rate limit exceeded: free-models-per-day", "code": 429}}))
    with pytest.raises(llm_client.LLMError, match="Rate limit exceeded"):
        llm_client._call_openrouter("deepseek/deepseek-chat", "q", "", 64, 0.0, 5)


def test_malformed_choices_raises_llmerror_not_keyerror(_key, monkeypatch):
    monkeypatch.setattr(llm_client.urllib.request, "urlopen", _urlopen_returning(
        {"choices": [{"unexpected": "shape"}]}))
    with pytest.raises(llm_client.LLMError, match="malformed"):
        llm_client._call_openrouter("deepseek/deepseek-chat", "q", "", 64, 0.0, 5)


def test_good_body_still_parses(_key, monkeypatch):
    monkeypatch.setattr(llm_client.urllib.request, "urlopen", _urlopen_returning(
        {"choices": [{"message": {"content": "hello"}}],
         "usage": {"prompt_tokens": 3, "completion_tokens": 1}}))
    text, tin, tout = llm_client._call_openrouter("deepseek/deepseek-chat", "q", "", 64, 0.0, 5)
    assert (text, tin, tout) == ("hello", 3, 1)
