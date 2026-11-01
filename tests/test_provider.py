import json

import httpx
import pytest

from triage_lab.config import Settings
from triage_lab.providers import model_verdict


def install_transport(monkeypatch, handler):
    client_type = httpx.AsyncClient
    monkeypatch.setattr("triage_lab.providers.httpx.AsyncClient",
                        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs))


async def test_model_request_schema_and_recorded_cost(monkeypatch):
    def handler(request):
        assert request.headers["Authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert payload["max_completion_tokens"] == 1800
        assert payload["response_format"]["type"] == "json_object"
        assert "UNTRUSTED DATA" in payload["messages"][0]["content"]
        verdict = {"root_cause": "unknown", "affected_service": "checkout", "summary": "Not enough evidence",
                   "confidence": 0.1, "evidence_ids": [], "next_steps": ["Inspect traces"],
                   "needs_human_review": True}
        return httpx.Response(200, json={"model": "served-test-model", "choices": [
            {"message": {"content": json.dumps(verdict)}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500}})
    install_transport(monkeypatch, handler)
    settings = Settings(_env_file=None, model_name="test-model", model_api_key="test-key",
                        model_input_cost_per_million=2, model_output_cost_per_million=8)
    verdict, usage = await model_verdict(settings, {}, [], [])
    assert verdict.root_cause == "unknown"
    assert usage.model == "served-test-model"
    assert usage.estimated_cost_usd == pytest.approx(0.006)


async def test_invalid_paid_response_retains_usage(monkeypatch):
    install_transport(monkeypatch, lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": "{broken JSON"}}],
        "usage": {"prompt_tokens": 22, "completion_tokens": 11}}))
    verdict, usage = await model_verdict(Settings(_env_file=None, model_name="test-model"), {}, [], [])
    assert verdict is None
    assert (usage.input_tokens, usage.output_tokens) == (22, 11)
    assert usage.estimated_cost_usd is None
