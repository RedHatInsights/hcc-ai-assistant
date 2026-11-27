import asyncio
import base64
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
from contextlib import asynccontextmanager, closing
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import psycopg2
import pytest
import uvicorn
import yaml
from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
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
    async def start(model_response=respond, stream_function=None, network=False):
        backend = create_app(model=FunctionModel(
            model_response, stream_function=stream_function))
        if network:
            async with loopback_server(backend) as upstream_url:
                monkeypatch.setattr(proxy, "BACKEND_URL", upstream_url)
                async with loopback_server(proxy.app) as proxy_url, httpx.AsyncClient(base_url=proxy_url,
                                                                                      trust_env=False) as client:
                    yield client
            return
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


async def test_agent_card_opt_in(start_backend, monkeypatch, calls):
    path = "/api/ai-assistant/.well-known/agent-card.json"
    monkeypatch.delenv("A2A_PUBLIC_URL", raising=False)
    async with start_backend() as client:
        assert (await client.get(path)).status_code == 404

    public_url = "https://gateway.example.test/api/ai-assistant/a2a"
    monkeypatch.setenv("A2A_PUBLIC_URL", public_url)
    async with start_backend() as client:
        response = await client.get(path)
        assert (await client.post("/api/ai-assistant/a2a", json={})).status_code == 404
    assert response.status_code == 200
    card = response.json()
    assert card["supportedInterfaces"] == [{"url": public_url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}]
    assert card["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]["scheme"] == "bearer"
    assert "bearer" in card["securityRequirements"][0]["schemes"]
    assert card["defaultInputModes"] == card["defaultOutputModes"] == ["text/plain"]
    assert not card["capabilities"].get("streaming")
    assert card["skills"][0]["id"] == "console-assistance"
    assert card["skills"][0]["examples"]
    assert calls == []


async def test_conversation_list_requires_identity(client, calls):
    response = await client.get("/api/ai-assistant/v1/conversations")
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/json"
    assert isinstance(response.json()["detail"], str)
    assert calls == []


def caller_headers(tenant="org-a", user_id="test-id", username="synthetic-user"):
    return {"x-rh-identity": encoded({"identity": {"type": "User", "org_id": tenant,
                                                   "user": {"user_id": user_id, "username": username}}})}


async def test_conversation_list_scoped_to_owner(client, calls):
    path = "/api/ai-assistant/v1/conversations"
    owner = caller_headers()
    assert (await client.get(path, headers=owner)).json() == {"conversations": []}
    created = await client.post("/api/ai-assistant/v1/query", headers=owner, json={"query": "What is HCC?"})
    assert created.status_code == 200
    calls.clear()
    listed = await client.get(path, headers=owner)
    assert listed.status_code == 200
    assert listed.json() == {"conversations": [
        {"conversation_id": created.json()["conversation_id"]}]}
    for other in (caller_headers(tenant="org-b"), caller_headers(user_id="another-user")):
        assert (await client.get(path, headers=other)).json() == {"conversations": []}
    assert calls == []


@asynccontextmanager
async def loopback_server(app):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(
            app, log_level="error", access_log=False))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                        pytest.fail("Loopback server exited before startup")
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            await task


async def test_welcome_prompt_stream_through_proxy(start_backend):
    release_answer = asyncio.Event()

    async def stream_answer(messages, info):
        yield "Synthetic "
        await asyncio.wait_for(release_answer.wait(), timeout=5)
        yield "console answer"

    request = {"query": "What can you help me with?", "media_type": "application/json",
               "model": "publishers/google/models/gemini-2.5-flash", "provider": "google-vertex",
               "generate_topic_summary": False}
    async with start_backend(stream_function=stream_answer, network=True) as client:
        path = "/api/ai-assistant/v1/streaming_query"
        assert (await client.post(path, json=request)).status_code == 401
        events = []
        async with client.stream("POST", path, headers=caller_headers(), json=request) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].split(
                ";")[0] == "application/json"
            lines = response.aiter_lines()
            async for line in lines:
                assert line.startswith("data: ")
                assert await anext(lines) == ""
                item = json.loads(line[6:])
                events.append(item)
                if item["event"] == "token":
                    release_answer.set()
        assert [item["event"] for item in events] == [
            "start", "token", "token", "end"]
        conversation_id = events[0]["data"]["conversation_id"]
        assert UUID(conversation_id).version == 4
        assert [item["data"] for item in events[1:-1]] == [
            {"id": 0, "token": "Synthetic "}, {"id": 1, "token": "console answer"}]
        assert events[-1]["data"]["truncated"] is False
        assert events[-1]["data"]["referenced_documents"] == []
        assert events[-1]["data"]["tool_calls"] == []
        assert isinstance(events[-1]["data"]["input_tokens"], int)
        assert isinstance(events[-1]["data"]["output_tokens"], int)
        assert events[-1]["available_quotas"] == {}
        followup = await client.post("/api/ai-assistant/v1/query", headers=caller_headers(),
                                     json=request | {"query": "And what else?", "conversation_id": conversation_id})
        assert followup.json()["response"] == "I remember the synthetic answer"


@pytest.fixture
def host_id():
    return "host-83c19"


@pytest.fixture
def lookup_error():
    return None


@pytest.fixture
def tool_servers(tmp_path, monkeypatch, host_id, lookup_error):
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "lightspeed-stack.yaml").read_text())
    config["mcp_servers"] = [
        {"name": "mcp-discovery-server",
            "url": "http://discovery.test/mcp", "headers": ["X-Trace"]},
        {"name": "inventory", "url": "http://inventory.test/mcp/",
            "headers": ["X-Rh-Identity"]},
        {"name": "advisor", "url": "http://advisor.test/mcp",
            "headers": ["X-Account"]},
    ]
    config_path = tmp_path / "servers.yaml"
    config_path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv("MCP_CONFIG_PATH", str(config_path))
    requests = []
    capability = {"server_name": "inventory", "tool_name": "lookup",
                  "description": "Find hosts", "input_schema": {}}

    async def dispatch(self, request):
        body = json.loads(await request.aread())
        method = body["method"]
        requests.append((request.url.host, method, dict(
            request.headers), body.get("params", {})))
        if method == "initialize":
            result = {"protocolVersion": body["params"]["protocolVersion"], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fixture", "version": "1"}}
        elif method == "notifications/initialized":
            return httpx.Response(202)
        elif method == "tools/list":
            name = {"inventory.test": "lookup", "advisor.test": "risk",
                    "discovery.test": "recommend_tools"}[request.url.host]
            result = {
                "tools": [{"name": name, "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            if request.url.host == "discovery.test":
                task = body["params"]["arguments"]["task"]
                choices = [capability]
                if task == "host risk":
                    choices.append({"server_name": "advisor", "tool_name": "risk", "description": "Get host risk",
                                    "input_schema": {}})
                text = f"Recommended {len(choices)} tools for task '{task}' (via vector):\n\n" + \
                    json.dumps(choices)
            elif request.url.host == "advisor.test":
                assert body["params"]["arguments"] == {"host_id": host_id}
                text = "elevated"
            else:
                assert request.url.host == "inventory.test"
                assert body["params"]["arguments"] == {"region": "west"}
                if lookup_error:
                    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                                     "error": {"code": -32000, "message": lookup_error}})
                text = host_id
            result = {"content": [{"type": "text", "text": text}]}
        else:
            pytest.fail(f"Unexpected MCP request: {body}")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    monkeypatch.setattr(httpx.AsyncHTTPTransport,
                        "handle_async_request", dispatch)
    return requests, capability


@pytest.mark.parametrize(("lookup_error", "expected_answer"), [
    (None, "host-83c19"),
    ("Permission denied", "Permission denied for this tool."),
    ("Internal error: synthetic-sensitive-detail", "The downstream tool could not complete the request."),
])
async def test_discovered_tool_answer_uses_current_caller(start_backend, tool_servers, lookup_error, expected_answer):
    requests, _ = tool_servers

    def respond(messages, info):
        model_input = repr((messages, info.instructions, info.model_settings))
        for value in ("synthetic-auth-only", "trace-first", "trace-second",
                      caller_headers(user_id="first")["x-rh-identity"], caller_headers(user_id="second")["x-rh-identity"]):
            assert value not in model_input
        part = messages[-1].parts[-1]
        if not info.function_tools and "Determine whether" in str(part.content):
            return ModelResponse(parts=[TextPart("ALLOWED")])
        if isinstance(part, UserPromptPart):
            assert "recommend_tools" in [
                tool.name for tool in info.function_tools]
            return ModelResponse(parts=[ToolCallPart("recommend_tools", {"task": "hosts"})])
        assert isinstance(part, ToolReturnPart)
        if part.tool_name == "recommend_tools":
            assert "lookup" in str(part.content)
            return ModelResponse(parts=[ToolCallPart("execute_tool", {"server_name": "inventory", "tool_name": "lookup",
                                                                      "arguments": {"region": "west"}})])
        return ModelResponse(parts=[TextPart(str(part.content))])

    async with start_backend(respond) as client:
        for caller in ("first", "second"):
            requests.clear()
            headers = caller_headers(user_id=caller) | {
                "x-trace": f"trace-{caller}", "authorization": "synthetic-auth-only"}
            response = await client.post("/api/ai-assistant/v1/query", headers=headers, json={"query": "Find my hosts"})
            assert response.status_code == 200
            assert response.json()["response"] == expected_answer
            assert [(host, params["name"]) for host, method, _, params in requests if method == "tools/call"] == [
                ("discovery.test", "recommend_tools"), ("inventory.test", "lookup")]
            for host, _, observed, _ in requests:
                expected = {
                    "x-trace": f"trace-{caller}"} if host == "discovery.test" else caller_headers(user_id=caller)
                assert {key: observed[key]
                        for key in headers if key in observed} == expected


@pytest.mark.parametrize("host_id", ["host-42a7", "host-6b90"])
async def test_dependent_tool_answer_in_one_turn(start_backend, tool_servers, host_id):
    requests, _ = tool_servers

    def respond(messages, info):
        part = messages[-1].parts[-1]
        if not info.function_tools:
            return ModelResponse(parts=[TextPart("ALLOWED")])
        if isinstance(part, UserPromptPart):
            return ModelResponse(parts=[ToolCallPart("recommend_tools", {"task": "host risk"})])
        assert isinstance(part, ToolReturnPart)
        if part.tool_name == "recommend_tools":
            assert {(item["server_name"], item["tool_name"]) for item in part.content} == {
                ("inventory", "lookup"), ("advisor", "risk")}
            return ModelResponse(parts=[ToolCallPart("execute_tool", {"server_name": "inventory", "tool_name": "lookup",
                                                                      "arguments": {"region": "west"}})])
        results = [item.content for message in messages for item in message.parts
                   if isinstance(item, ToolReturnPart) and item.tool_name == "execute_tool"]
        if len(results) == 1:
            return ModelResponse(parts=[ToolCallPart("execute_tool", {"server_name": "advisor", "tool_name": "risk",
                                                                      "arguments": {"host_id": results[0]}})])
        resource, risk = results
        return ModelResponse(parts=[TextPart(f"{resource} has {risk} risk")])

    identity = caller_headers()
    policies = {"inventory.test": identity, "advisor.test": {"x-account": "test-account"},
                "discovery.test": {"x-trace": "test-trace"}}
    headers = identity | policies["advisor.test"] | policies["discovery.test"] | {
        "authorization": "not-forwarded"}
    async with start_backend(respond) as client:
        response = await client.post("/api/ai-assistant/v1/query", headers=headers,
                                     json={"query": "What is the risk for my western host?"})
    assert response.status_code == 200
    assert response.json()["response"] == f"{host_id} has elevated risk"
    assert response.json()["tool_calls"] == [
        {"server_name": "mcp-discovery-server", "tool_name": "recommend_tools"},
        {"server_name": "inventory", "tool_name": "lookup"},
        {"server_name": "advisor", "tool_name": "risk"},
    ]
    assert [(host, params["name"]) for host, method, _, params in requests if method == "tools/call"] == [
        ("discovery.test", "recommend_tools"), ("inventory.test", "lookup"), ("advisor.test", "risk")]
    for host, _, observed, _ in requests:
        assert {key: observed[key]
                for key in headers if key in observed} == policies[host]


@pytest.mark.parametrize(("recommended_server", "recommended_tool", "target", "tool"), [
    ("inventory", "gone", "inventory", "gone"),
    ("inventory", "lookup", "inventory", "other"),
    ("http://unregistered.test", "lookup", "http://unregistered.test", "lookup"),
    ("inventory", "lookup", "http://inventory.test/mcp/", "lookup"),
])
async def test_only_exact_available_recommendation_can_execute(start_backend, tool_servers, recommended_server,
                                                               recommended_tool, target, tool):
    requests, capability = tool_servers
    capability.update(server_name=recommended_server,
                      tool_name=recommended_tool)

    def respond(messages, info):
        part = messages[-1].parts[-1]
        if not info.function_tools:
            return ModelResponse(parts=[TextPart("ALLOWED")])
        if isinstance(part, UserPromptPart):
            return ModelResponse(parts=[ToolCallPart("recommend_tools", {"task": "hosts"})])
        if isinstance(part, ToolReturnPart) and part.tool_name == "recommend_tools":
            selection = {"server_name": target,
                         "tool_name": tool, "arguments": {}}
            return ModelResponse(parts=[ToolCallPart("execute_tool", selection)])
        return ModelResponse(parts=[TextPart("No available tool")])

    async with start_backend(respond) as client:
        response = await client.post("/api/ai-assistant/v1/query", headers=caller_headers(), json={"query": "hosts"})
        assert response.json()["response"] == "No available tool"
    for host, method, _, _ in requests:
        if host != "discovery.test":
            assert host == "inventory.test" and tool == "gone" and method != "tools/call"


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
        history, = conn.execute(
            "SELECT history FROM agent_conversations_v1").fetchone()
    assert expected in history
    for value in ("example@example.test", "123-45-6789", "mock-pass", "synthetic_token_1234", "AAAAtest"):
        assert value not in repr(calls) and value not in history


async def test_followup_requires_new_recommendation(start_backend, tool_servers):
    requests, _ = tool_servers

    def respond(messages, info):
        part = messages[-1].parts[-1]
        if not info.function_tools:
            return ModelResponse(parts=[TextPart("ALLOWED")])
        if isinstance(part, UserPromptPart):
            if part.content == "Find tools":
                return ModelResponse(parts=[ToolCallPart("recommend_tools", {"task": "hosts"})])
            return ModelResponse(parts=[ToolCallPart("execute_tool", {"server_name": "inventory", "tool_name": "lookup",
                                                                      "arguments": {"region": "west"}})])
        return ModelResponse(parts=[TextPart(str(part.content))])

    path = "/api/ai-assistant/v1/query"
    async with start_backend(respond) as client:
        first = await client.post(path, headers=caller_headers(), json={"query": "Find tools"})
        assert "lookup" in first.json()["response"]
        requests.clear()
        followup = await client.post(path, headers=caller_headers(), json={"query": "Use the same tool",
                                                                           "conversation_id": first.json()["conversation_id"]})
        assert followup.status_code == 200
        assert followup.json()["tool_calls"] == []
        assert requests == []


@pytest.mark.parametrize("verdict", ["REJECTED", "not ALLOWED", ""])
async def test_topic_refusal(tool_servers, client, calls, verdict):
    response = await client.post("/api/ai-assistant/v1/query", headers=caller_headers(),
                                 json={"query": "Write a cake recipe for example@example.test"})
    assert response.status_code == 200
    body = response.json()
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "lightspeed-stack.yaml").read_text())
    guard = next(shield["config"] for shield in config["shields"]
                 if shield["name"] == "hcc-topic-guard")
    assert body["response"] == guard["invalid_question_response"]
    assert body["tool_calls"] == body["tool_results"] == []
    assert UUID(body["conversation_id"]).version == 4
    assert len(calls) == 1
    prompt = calls[0][0][-1].parts[-1].content
    assert "[REDACTED_EMAIL]" in prompt and "example@example.test" not in prompt
    assert "ALLOWED" in prompt and "REJECTED" in prompt and "${" not in prompt
    assert tool_servers[0] == []


async def test_restart_applies_current_rules_to_prior_history(start_backend, calls, tmp_path, monkeypatch):
    path = "/api/ai-assistant/v1/query"
    async with start_backend() as client:
        first = await client.post(path, headers=caller_headers(), json={"query": "Explain HCC for private-team"})
        conversation_id = first.json()["conversation_id"]
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "lightspeed-stack.yaml").read_text())
    rules = next(shield["config"]["rules"]
                 for shield in config["shields"] if shield["name"] == "pii-redaction")
    rules.append(
        {"pattern": "private-team|Synthetic console answer", "replacement": "[PRIVATE]"})
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
        assert "private-team" not in repr(
            messages) and "Synthetic console answer" not in repr(messages)
    with closing(sqlite3.connect(tmp_path / "chat.sqlite")) as conn:
        history, = conn.execute(
            "SELECT history FROM agent_conversations_v1").fetchone()
    assert "private-team" not in history


@pytest.fixture
def isolated_postgres(monkeypatch, tmp_path):
    if not all(shutil.which(tool) for tool in ("initdb", "pg_ctl")):
        pytest.skip(
            "add PostgreSQL server binaries to PATH to run the disposable cluster check")
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
            subprocess.run(["pg_ctl", "-D", data, "-m",
                           "fast", "-w", "stop"], check=True)


@pytest.mark.asyncio
async def test_postgres_owned_restart_preserves_existing_data(start_backend, isolated_postgres, calls):
    with closing(psycopg2.connect()) as conn, conn, conn.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE conversations AS SELECT 'legacy chat' AS value")
        cursor.execute(
            "CREATE TABLE embeddings AS SELECT 'legacy embedding' AS value")
    path = "/api/ai-assistant/v1/query"
    async with start_backend() as client:
        first = await client.post(path, headers=caller_headers(), json={"query": "What is HCC?"})
        assert first.status_code == 200
        conversation_id = first.json()["conversation_id"]
    async with start_backend() as restarted:
        body = {"query": "What did you say?",
                "conversation_id": conversation_id}
        denied = await restarted.post(path, headers=caller_headers(tenant="org-b"), json=body)
        assert denied.status_code == 404
        assert len(calls) == 2
        resumed = await restarted.post(path, headers=caller_headers(), json=body)
        assert resumed.status_code == 200
        assert resumed.json()["conversation_id"] == conversation_id
        assert resumed.json()["response"] == "I remember the synthetic answer"
    with closing(psycopg2.connect()) as conn, conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT conversations.value, embeddings.value FROM conversations CROSS JOIN embeddings")
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
