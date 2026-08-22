"""AI price-extraction fallback — offline via httpx.MockTransport.

No network, no new test deps: ``AiClient`` accepts an injected
``httpx.AsyncBaseTransport`` (its constructor's ``transport`` param), so every
provider behavior — 200s, 429/503 retries, terminal 400s, malformed bodies —
is scripted in-process. Backoff sleeps are fast-forwarded by monkeypatching
``argus.ai._sleep`` (the module's own indirection point).
"""

from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from argus import ai as ai_mod
from argus.ai import AiClient, PriceExtraction, build_ai_client, reduce_html
from argus.config import Settings


def _settings(**overrides: object) -> Settings:
    values: dict = {
        "ai_base_url": "https://ai.test/v1",
        "ai_api_key": "test-key",
        "ai_model": "test-model",
        "ai_zen_host": "",
        "ai_user_agent": "",
        "ai_client_header": "",
        "ai_concurrency": 1,
        "ai_min_interval_ms": 0,
        "ai_max_retries": 3,
    }
    values.update(overrides)
    return Settings.model_construct(**values)


_OK_BODY = '{"price":119,"currency":"NZD","name":"Widget","available":true}'


@pytest.fixture(autouse=True)
def _fast_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset throttle state per test and fast-forward every backoff sleep."""
    ai_mod._reset_throttle()

    async def _nosleep(seconds: float) -> None:  # noqa: ARG001 — signature match
        return None

    monkeypatch.setattr(ai_mod, "_sleep", _nosleep)
    yield
    ai_mod._reset_throttle()


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> AiClient:
    return AiClient(_settings(), transport=httpx.MockTransport(handler))


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
    )


# --- happy paths -----------------------------------------------------------------


async def test_happy_path_available() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        auth = request.headers["Authorization"]
        assert auth == "Bearer test-key"  # key rides the header...
        return _completion(_OK_BODY)

    result = await _client(handler).extract_price("<html><body>x</body></html>", "https://e.test/p")
    assert result == PriceExtraction(
        available=True, price="119.00", currency="NZD", name="Widget"
    )


async def test_unavailable_branch_tolerates_nulls() -> None:
    body = '{"price":null,"currency":null,"name":null,"available":false}'
    result = await _client(lambda r: _completion(body)).extract_price("<html></html>", "u")
    assert result == PriceExtraction(available=False, price=None, currency=None, name=None)


async def test_prose_wrapped_json_parses() -> None:
    wrapped = f'Sure! Here it is:\n```json\n{_OK_BODY}\n```\nhope that helps'
    result = await _client(lambda r: _completion(wrapped)).extract_price("<html></html>", "u")
    assert result is not None and result.price == "119.00"


async def test_price_normalized_to_two_decimals() -> None:
    # Model contract is JSON numbers (iris types.ts parity); the 2dp Decimal
    # normalization is argus-side.
    result = await _client(
        lambda r: _completion('{"price":1205.5,"currency":"NZD","available":true}')
    ).extract_price("<html></html>", "u")
    assert result is not None and result.price == "1205.50"


# --- retry semantics --------------------------------------------------------------


async def test_429_then_200_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "rate limit exceeded"})
        return _completion(_OK_BODY)

    # Observe the backoff wait without sleeping for real.
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(ai_mod, "_sleep", fake_sleep)
    result = await _client(handler).extract_price("<html></html>", "u")
    assert calls["n"] == 2
    assert result is not None and result.available is True
    assert len(waits) >= 1  # exponential+jitter backoff actually scheduled


async def test_503_then_200_retries() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="Service Unavailable")
        return _completion(_OK_BODY)

    result = await _client(handler).extract_price("<html></html>", "u")
    assert calls["n"] == 2
    assert result is not None and result.price == "119.00"


async def test_400_is_terminal_single_call() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad request"})

    result = await _client(handler).extract_price("<html></html>", "u")
    assert calls["n"] == 1  # no retry on schema/validation-class errors
    assert result is None


async def test_retries_exhausted_returns_none() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="Service Unavailable")

    result = await _client(handler).extract_price("<html></html>", "u")
    assert calls["n"] == 3  # ai_max_retries attempts, then give up
    assert result is None


# --- malformed model output -------------------------------------------------------


async def test_schema_violation_terminal_and_none() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # available=true demands a positive price; -5 violates the union.
        return _completion('{"price":-5,"currency":"NZD","available":true}')

    result = await _client(handler).extract_price("<html></html>", "u")
    assert calls["n"] == 1  # formatting problems don't deserve a second call
    assert result is None


async def test_malformed_provider_body_maps_to_none() -> None:
    result = await _client(
        lambda r: httpx.Response(200, text="this is not json at all")
    ).extract_price("<html></html>", "u")
    assert result is None


async def test_missing_choices_maps_to_none() -> None:
    result = await _client(lambda r: httpx.Response(200, json={"oops": True})).extract_price(
        "<html></html>", "u"
    )
    assert result is None


# --- request shaping ---------------------------------------------------------------


async def test_zen_parity_headers_sent_when_host_matches() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("User-Agent", "")
        seen["client"] = request.headers.get("X-Opencode-Client", "")
        return _completion(_OK_BODY)

    client = AiClient(
        _settings(
            ai_base_url="https://opencode.ai/zen/v1",
            ai_zen_host="opencode.ai",
            ai_user_agent="opencode/1.18.12",
            ai_client_header="cli",
        ),
        transport=httpx.MockTransport(handler),
    )
    await client.extract_price("<html></html>", "u")
    assert seen["ua"] == "opencode/1.18.12"
    assert seen["client"] == "cli"


async def test_prompt_carries_url_and_reduced_page() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        captured["prompt"] = body["messages"][0]["content"]
        captured["model"] = body["model"]
        return _completion(_OK_BODY)

    await _client(handler).extract_price(
        "<html><body><h1>Widget Shop</h1></body></html>", "https://e.test/widget"
    )
    assert "https://e.test/widget" in captured["prompt"]
    assert "Widget Shop" in captured["prompt"]
    assert "PAGE CONTENT:" in captured["prompt"]
    assert captured["model"] == "test-model"


# --- reduce_html ---------------------------------------------------------------------


def test_reduce_html_strips_scripts_from_visible_text() -> None:
    html = (
        "<html><head><style>.x{color:red}</style>"
        "<script>var notVisible=true;</script></head>"
        "<body><h1>Hello</h1><p>World</p></body></html>"
    )
    reduced = reduce_html(html)
    assert "Hello World" in reduced
    assert "notVisible" not in reduced.split("EMBEDDED PRICE DATA")[0]
    assert ".x{color:red}" not in reduced


def test_reduce_html_embeds_price_bearing_blobs() -> None:
    html = (
        "<html><body>Data</body>"
        "<script>var config={\"formattedValue\":\"$119.00\",\"currency\":\"NZD\"};</script>"
        "</html>"
    )
    reduced = reduce_html(html)
    assert "EMBEDDED PRICE DATA:" in reduced
    assert "formattedValue" in reduced


def test_reduce_html_caps_visible_text_and_blob_count() -> None:
    long_text = "<body>" + "word " * 5000 + "</body>"  # ~25k chars visible
    blobs = "".join(
        f"<script>var price{i}={{'price':{i}}};</script>" for i in range(6)
    )
    reduced = reduce_html(long_text + blobs)
    visible = reduced.split("EMBEDDED PRICE DATA")[0]
    assert len(visible.replace("VISIBLE TEXT:", "").strip()) <= 8_000
    assert reduced.count("---") <= 2  # at most 3 blobs → at most 2 separators
    assert "'price':5" not in reduced  # blob #6 dropped (cap = 3)


# --- builder / configuration gate ------------------------------------------------------


def test_builder_none_when_unconfigured() -> None:
    assert build_ai_client(_settings(ai_api_key="")) is None
    assert build_ai_client(_settings(ai_base_url="")) is None
    assert build_ai_client(_settings(ai_model="")) is None


def test_builder_constructs_when_fully_configured() -> None:
    client = build_ai_client(_settings())
    assert isinstance(client, AiClient)
