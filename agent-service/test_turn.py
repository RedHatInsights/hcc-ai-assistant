import base64
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
import yaml
from pydantic_ai.messages import ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from turn import create_app

import proxy


@pytest.fixture
def calls():
    return []


@pytest.fixture
async def client(monkeypatch, calls):
    def respond(messages, info):
        calls.append((messages, info.instructions))
        return ModelResponse(parts=[TextPart("Synthetic console answer")])

    backend = create_app(model=FunctionModel(respond))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=backend)) as upstream:
        monkeypatch.setattr(proxy.app, "state",
                            SimpleNamespace(client=upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://proxy") as client:
            yield client


@pytest.mark.asyncio
async def test_missing_identity_rejected(client, calls):
    response = await client.post("/api/ai-assistant/v1/query", json={"query": "What is HCC?"})
    assert response.status_code == 401
    assert isinstance(response.json()["detail"], str)
    assert calls == []


def encoded(data):
    return base64.b64encode(json.dumps(data).encode()).decode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header", "detail"),
    [
        ("A" * 8193, "x-rh-identity header exceeds maximum allowed size"),
        ("not-base64!", "Invalid base64 encoding in x-rh-identity header"),
        ("bm90LWpzb24=", "Invalid JSON in x-rh-identity header"),
        (encoded(None), "Invalid identity data"),
        (encoded({"identity": {"type": "User", "user": {
         "user_id": "test"}}}), "Invalid identity data"),
        (
            encoded({"identity": {"type": "User", "user": {
                    "username": " ", "user_id": "test"}}}),
            "Invalid identity data",
        ),
    ],
)
async def test_invalid_identity_never_reaches_model(client, calls, header, detail):
    response = await client.post(
        "/api/ai-assistant/v1/query", headers={"x-rh-identity": header}, json={"query": "What is HCC?"}
    )
    assert response.status_code == 400
    assert response.json() == {"detail": detail}
    assert calls == []


@pytest.mark.asyncio
async def test_proxy_health_tracks_backend_availability(client, calls, monkeypatch):
    assert (await client.get("/readiness")).status_code == 200

    def disconnected(request):
        raise httpx.ConnectError("test backend offline", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(disconnected)) as offline:
        with monkeypatch.context() as outage:
            outage.setattr(proxy.app.state, "client", offline)
            assert (await client.get("/liveness")).status_code == 200
            assert (await client.get("/readiness")).status_code == 503
    assert calls == []


@pytest.mark.asyncio
async def test_first_console_turn(client, calls):
    identity = encoded({"identity": {"type": "User", "user": {
                       "username": "synthetic-user", "user_id": "test-id"}}})
    response = await client.post(
        "/api/ai-assistant/v1/query",
        headers={"x-rh-identity": identity},
        json={"query": "What is HCC?", "provider": "google-vertex",
              "model": "google-vertex/google/gemini-2.5-flash"},
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "conversation_id",
        "response",
        "input_tokens",
        "output_tokens",
        "truncated",
        "rag_chunks",
        "referenced_documents",
        "tool_calls",
        "tool_results",
        "available_quotas",
    }
    assert UUID(body["conversation_id"]).version == 4
    assert body["response"] == "Synthetic console answer"
    assert body["tool_calls"] == body["tool_results"] == []
    ((messages, instructions),) = calls
    assert [part.content for msg in messages for part in msg.parts if isinstance(part, UserPromptPart)] == [
        "What is HCC?"
    ]
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "lightspeed-stack.yaml").read_text())
    assert instructions == config["customization"]["system_prompt"]
    assert "synthetic-user" not in repr(
        messages) and identity not in repr(messages)
