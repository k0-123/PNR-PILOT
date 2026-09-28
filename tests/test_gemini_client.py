import pytest
from google.genai import errors
from pydantic import SecretStr

from app.core.costs import Prices, cost_usd, extract_prices, result_prices
from app.core.models import GeminiExtraction
from app.core.retry import backoff_delay
from app.extraction.gemini_client import (
    GeminiClient, GeminiError, GeminiParseError, MalformedResponseError, parse_structured,
)
from tests.conftest import gemini_row

P = Prices(0.30, 2.50)


def call(client, **kw):
    records = []
    result = client.generate_json(["x"], GeminiExtraction, model="m", prices=P, purpose="t",
                                  on_call=records.append, **kw)
    return result, records


# ---- parsing ----

@pytest.mark.parametrize("text", [
    None, "", "   ", "not json", '{"rows": [', '{"rows": "nope"}', '{"nope": []}',
    '{"rows": [{"pax_count": "abc"}]}',
])
def test_parse_rejects_malformed(text):
    with pytest.raises(MalformedResponseError):
        parse_structured(text, GeminiExtraction)


def test_parse_accepts_fences_bare_list_and_empty_rows():
    assert parse_structured('```json\n{"rows": [{"surname": "A"}]}\n```', GeminiExtraction).rows[0].surname == "A"
    assert len(parse_structured('[{"surname": "A"}, {}]', GeminiExtraction).rows) == 2
    assert parse_structured('{"rows": []}', GeminiExtraction).rows == []


# ---- malformed JSON: retried exactly once ----

def test_malformed_json_retried_once_then_ok(make_client):
    client, fake = make_client(["garbage", {"rows": [gemini_row()]}])
    result, records = call(client)
    assert len(result.data.rows) == 1 and result.attempts == 2
    assert [r.success for r in records] == [False, True]
    assert (result.tokens_in, result.tokens_out) == (2000, 400)  # both attempts billed


def test_malformed_json_twice_is_parse_error(make_client):
    client, fake = make_client(["garbage", '{"rows": 5}', {"rows": []}])
    with pytest.raises(GeminiParseError) as ei:
        call(client)
    assert len(fake.calls) == 2
    assert ei.value.raw_text == '{"rows": 5}' and ei.value.attempts == 2


# ---- transient errors: backoff up to GEMINI_MAX_RETRIES ----

def server_error(code=503):
    return errors.ServerError(code, {"error": {"message": "unavailable", "status": "UNAVAILABLE"}})


def client_error(code):
    return errors.ClientError(code, {"error": {"message": "bad", "status": "X"}})


def test_retries_5xx_429_and_timeouts(make_client):
    client, fake = make_client([server_error(), client_error(429), TimeoutError(), {"rows": []}])
    result, records = call(client)
    assert result.data.rows == [] and result.attempts == 4
    assert [r.success for r in records] == [False, False, False, True]


def test_gives_up_after_max_retries(make_client, settings):
    client, fake = make_client([server_error()] * 10)
    with pytest.raises(GeminiError):
        call(client)
    assert len(fake.calls) == settings.gemini_max_retries + 1


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_no_retry_on_permanent_client_errors(make_client, code):
    client, fake = make_client([client_error(code), {"rows": []}])
    with pytest.raises(GeminiError):
        call(client)
    assert len(fake.calls) == 1


def test_backoff_grows_exponentially():
    for attempt in range(1, 5):
        d = backoff_delay(attempt, base=1, cap=100)
        assert 0.8 * 2 ** (attempt - 1) <= d <= 1.2 * 2 ** (attempt - 1)
    assert backoff_delay(20, base=1, cap=30) <= 36


def test_request_config(make_client):
    client, fake = make_client([{"rows": []}])
    call(client)
    c = fake.calls[0]
    assert c["model"] == "m"
    assert c["config"].response_mime_type == "application/json"
    assert c["config"].response_schema is GeminiExtraction
    assert c["config"].temperature == 0
    assert c["config"].thinking_config.thinking_budget == 0


# ---- costs ----

def test_cost_calculation(settings):
    assert cost_usd(1_000_000, 1_000_000, P) == pytest.approx(2.80)
    assert cost_usd(1000, 200, P) == pytest.approx(0.0008)
    settings.price_result_input_per_m, settings.price_result_output_per_m = 0.10, 0.40
    assert result_prices(settings) == Prices(0.10, 0.40)
    assert extract_prices(settings) == Prices(0.30, 2.50)


def test_record_costs_per_attempt(make_client):
    client, _ = make_client([{"rows": []}])
    result, records = call(client)
    assert records[0].cost_usd == pytest.approx(0.0008) == result.cost_usd


# ---- secrets ----

def test_missing_api_key(settings):
    settings.gemini_api_key = SecretStr("")
    with pytest.raises(GeminiError, match="GEMINI_API_KEY"):
        GeminiClient(settings)


def test_api_key_not_in_settings_repr(settings):
    assert "test-secret-key" not in repr(settings) and "test-secret-key" not in str(settings)
