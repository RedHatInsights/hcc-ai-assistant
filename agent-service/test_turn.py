import base64
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import psycopg2
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
def verdict():
    return "ALLOWED"


@pytest.fixture
def start_backend(monkeypatch, calls, tmp_path, verdict):
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.setenv("AGENT_SQLITE_PATH", str(tmp_path / "chat.sqlite"))

    def respond(messages, info):
        calls.append((messages, info.instructions))
        if messages[-1].parts[-1].content.startswith("Determine whether the user's question"):
            assert not info.function_tools
            return ModelResponse(parts=[TextPart(verdict)])
        if any(isinstance(part, TextPart) and part.content == "Synthetic console answer"
               for message in messages for part in message.parts):
            return ModelResponse(parts=[TextPart("I remember the synthetic answer")])
        return ModelResponse(parts=[TextPart("Synthetic console answer")])

    @asynccontextmanager
    async def start():
        backend = create_app(model=FunctionModel(respond))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=backend)) as upstream:
            monkeypatch.setattr(proxy.app, "state",
                                SimpleNamespace(client=upstream))
            transport = httpx.ASGITransport(app=proxy.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
                yield client

    return start


@pytest.fixture
async def client(start_backend):
    async with start_backend() as client:
        yield client


@pytest.mark.asyncio
async def test_missing_identity_rejected(client, calls):
    response = await client.post("/api/ai-assistant/v1/query", json={"query": "What is HCC?"})
    assert response.status_code == 401
    assert isinstance(response.json()["detail"], str)
    assert calls == []


def encoded(data):
    return base64.b64encode(json.dumps(data).encode()).decode()


def caller_headers(tenant="org-a", user_id="test-id", username="synthetic-user"):
    return {"x-rh-identity": encoded({"identity": {"type": "User", "org_id": tenant,
                                                   "user": {"user_id": user_id, "username": username}}})}


async def test_sensitive_console_turn(client, calls, tmp_path):
    values = ["example@example.test", "123-45-6789", "PASSWD=mock-pass", "secret:synthetic_token_1234",
              "ssh-ed25519 AAAAtest"]
    expected = "HCC: [REDACTED_EMAIL] [REDACTED_SSN] [REDACTED_PASSWORD] [REDACTED_SECRET] [REDACTED_SSH_KEY]"
    response = await client.post("/api/ai-assistant/v1/query", headers=caller_headers(),
                                 json={"query": "HCC: " + " ".join(values)})
    assert response.json()["response"] == "Synthetic console answer"
    assert len(calls) == 2
    assert expected in calls[0][0][-1].parts[-1].content
    assert calls[-1][0][-1].parts[-1].content == expected
    with closing(sqlite3.connect(tmp_path / "chat.sqlite")) as conn:
        history, = conn.execute("SELECT history FROM agent_conversations_v1").fetchone()
    assert expected in history
    for value in ("example@example.test", "123-45-6789", "mock-pass", "synthetic_token_1234", "AAAAtest"):
        assert value not in repr(calls) and value not in history


@pytest.mark.parametrize("verdict", ["REJECTED", "not ALLOWED", ""])
async def test_topic_refusal(client, calls, verdict):
    response = await client.post("/api/ai-assistant/v1/query", headers=caller_headers(),
                                 json={"query": "Write a cake recipe for example@example.test"})
    assert response.status_code == 200
    body = response.json()
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "lightspeed-stack.yaml").read_text())
    guard = next(shield["config"] for shield in config["shields"] if shield["name"] == "hcc-topic-guard")
    assert body["response"] == guard["invalid_question_response"]
    assert body["tool_calls"] == body["tool_results"] == []
    assert UUID(body["conversation_id"]).version == 4
    assert len(calls) == 1
    prompt = calls[0][0][-1].parts[-1].content
    assert "[REDACTED_EMAIL]" in prompt and "example@example.test" not in prompt
    assert "ALLOWED" in prompt and "REJECTED" in prompt and "${" not in prompt


async def test_restart_applies_current_rules_to_prior_history(start_backend, calls, tmp_path, monkeypatch):
    path = "/api/ai-assistant/v1/query"
    async with start_backend() as client:
        first = await client.post(path, headers=caller_headers(), json={"query": "Explain HCC for private-team"})
        conversation_id = first.json()["conversation_id"]
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "lightspeed-stack.yaml").read_text())
    rules = next(shield["config"]["rules"] for shield in config["shields"] if shield["name"] == "pii-redaction")
    rules.append({"pattern": "private-team|Synthetic console answer", "replacement": "[PRIVATE]"})
    config_path = tmp_path / "policy.yaml"
    config_path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv("MCP_CONFIG_PATH", str(config_path))
    calls.clear()
    async with start_backend() as restarted:
        response = await restarted.post(path, headers=caller_headers(), json={"query": "And RHEL?",
                                                                             "conversation_id": conversation_id})
    assert response.status_code == 200
    assert "Explain HCC for [PRIVATE]" in repr(calls)
    for messages, _ in calls:
        assert "private-team" not in repr(messages) and "Synthetic console answer" not in repr(messages)
    with closing(sqlite3.connect(tmp_path / "chat.sqlite")) as conn:
        history, = conn.execute("SELECT history FROM agent_conversations_v1").fetchone()
    assert "private-team" not in history


@pytest.fixture
def isolated_postgres(monkeypatch, tmp_path):
    if not all(shutil.which(tool) for tool in ("initdb", "pg_ctl")):
        pytest.skip("add PostgreSQL server binaries to PATH to run the disposable cluster check")
    for name in list(os.environ):
        if name.startswith("PG"):
            monkeypatch.delenv(name)
    data = tmp_path / "pgdata"
    subprocess.run(["initdb", "-D", data, "-A", "trust", "-U", "test_user", "--no-locale"], check=True,
                   stdout=subprocess.DEVNULL)
    with tempfile.TemporaryDirectory(prefix="agent-pg-", dir="/tmp") as socket_dir:
        monkeypatch.setenv("PGHOST", socket_dir)
        monkeypatch.setenv("PGDATABASE", "postgres")
        monkeypatch.setenv("PGUSER", "test_user")
        subprocess.run(["pg_ctl", "-D", data, "-l", tmp_path / "pg.log", "-o",
                        f"-c listen_addresses='' -k {socket_dir}", "-w", "start"], check=True)
        try:
            yield
        finally:
            subprocess.run(["pg_ctl", "-D", data, "-m", "fast", "-w", "stop"], check=True)


@pytest.mark.asyncio
async def test_postgres_owned_restart_preserves_existing_data(start_backend, isolated_postgres, calls):
    with closing(psycopg2.connect()) as conn, conn, conn.cursor() as cursor:
        cursor.execute("CREATE TABLE conversations AS SELECT 'legacy chat' AS value")
        cursor.execute("CREATE TABLE embeddings AS SELECT 'legacy embedding' AS value")
    path = "/api/ai-assistant/v1/query"
    async with start_backend() as client:
        first = await client.post(path, headers=caller_headers(), json={"query": "What is HCC?"})
        assert first.status_code == 200
        conversation_id = first.json()["conversation_id"]
    async with start_backend() as restarted:
        body = {"query": "What did you say?", "conversation_id": conversation_id}
        denied = await restarted.post(path, headers=caller_headers(tenant="org-b"), json=body)
        assert denied.status_code == 404
        assert len(calls) == 2
        resumed = await restarted.post(path, headers=caller_headers(), json=body)
        assert resumed.status_code == 200
        assert resumed.json()["conversation_id"] == conversation_id
        assert resumed.json()["response"] == "I remember the synthetic answer"
    with closing(psycopg2.connect()) as conn, conn, conn.cursor() as cursor:
        cursor.execute("SELECT conversations.value, embeddings.value FROM conversations CROSS JOIN embeddings")
        assert cursor.fetchall() == [("legacy chat", "legacy embedding")]
        cursor.execute("SELECT owner_key, history FROM agent_conversations_v1 WHERE conversation_id = %s",
                   (conversation_id,))
        owner, history = cursor.fetchone()
        for claim in ("org-a", "test-id", "synthetic-user", caller_headers()["x-rh-identity"]):
            assert claim not in owner and claim not in history


@pytest.mark.asyncio
async def test_continue_after_restart(start_backend):
    path = "/api/ai-assistant/v1/query"
    async with start_backend() as client:
        first = await client.post(path, headers=caller_headers(), json={"query": "What is HCC?"})
        assert first.status_code == 200
        conversation_id = first.json()["conversation_id"]
    async with start_backend() as restarted:
        followup = await restarted.post(path, headers=caller_headers(username="renamed"),
                                        json={"query": "What did you say?", "conversation_id": conversation_id})
        assert followup.status_code == 200
        assert followup.json()["response"] == "I remember the synthetic answer"
        assert followup.json()["conversation_id"] == conversation_id


@pytest.mark.asyncio
async def test_private_conversation(client, calls):
    path = "/api/ai-assistant/v1/query"
    first = await client.post(path, headers=caller_headers(), json={"query": "What is HCC?"})
    assert first.status_code == 200
    followup = {"query": "Recall the answer",
                "conversation_id": first.json()["conversation_id"]}
    missing = await client.post(path, headers=caller_headers(), json={**followup, "conversation_id": str(uuid4())})
    assert missing.status_code == 404
    for headers in (caller_headers(tenant="org-b"), caller_headers(user_id="another-user")):
        denied = await client.post(path, headers=headers, json=followup)
        assert (denied.status_code, denied.json()) == (
            missing.status_code, missing.json())
    assert len(calls) == 2
    resumed = await client.post(path, headers=caller_headers(), json=followup)
    assert resumed.json()["response"] == "I remember the synthetic answer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header", "detail"),
    [
        ("A" * 8193, "x-rh-identity header exceeds maximum allowed size"),
        ("not-base64!", "Invalid base64 encoding in x-rh-identity header"),
        ("bm90LWpzb24=", "Invalid JSON in x-rh-identity header"),
        (encoded(None), "Invalid identity data"),
        (encoded({"identity": {"type": "User", "user": {
            "user_id": "test-id", "username": "synthetic-user"}}}), "Invalid identity data"),
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
    identity = caller_headers()["x-rh-identity"]
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
    _, (messages, instructions) = calls
    assert [part.content for msg in messages for part in msg.parts if isinstance(part, UserPromptPart)] == [
        "What is HCC?"
    ]
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "lightspeed-stack.yaml").read_text())
    assert instructions == config["customization"]["system_prompt"]
    assert "synthetic-user" not in repr(
        messages) and identity not in repr(messages)
