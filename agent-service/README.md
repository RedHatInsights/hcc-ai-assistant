# Opt-in first-turn test backend

The default deployment still runs lcore. This service is for a trusted, isolated
test deployment only. It trusts a gateway-supplied `x-rh-identity`; base64 is not
authentication. Do not expose it to untrusted callers or use real account data.
Only synthetic console User identities and fresh synchronous turns are in scope.
There is no persistence, tool access, input redaction, topic gate, or streaming.

From the repository root, run the offline proxy tests:

```sh
uv run --project agent-service --locked pytest -q agent-service/test_turn.py
```

For an approved Vertex test, set `VERTEX_PROJECT`, `VERTEX_LOCATION`, and
`ALLOWED_MODEL` to the test Gemini deployment. Configure Application Default
Credentials (for example via `GOOGLE_APPLICATION_CREDENTIALS`); do not include
credentials in requests. `MCP_CONFIG_PATH` optionally selects a rendered stack
YAML; otherwise the agent reads `customization.system_prompt` from the repository
template. In separate terminals, on a host with both ports available:

```sh
uv run --project agent-service --locked uvicorn turn:create_app --factory --app-dir agent-service --host 127.0.0.1 --port 8080
PROXY_BACKEND_URL=http://127.0.0.1:8080 uv run --project agent-service --locked uvicorn proxy:app --host 127.0.0.1 --port 8000
```

Use `POST /api/ai-assistant/v1/query` with a JSON `query` and a trusted test
identity. Only the question and configured instructions go to the model.
`GET /readiness` means the proxy can reach the initialized agent, not that Vertex
has accepted credentials or has quota. `GET /liveness` is independent of the agent.

## Provisional contract

Local lightspeed-stack revision `634d32eb9affba8b7022ed4dd8efc8bd0fac345a`:
`docs/devel_doc/openapi.json`, `src/models/api/responses/successful/query.py`,
and `src/authentication/rh_identity.py` supply the synchronous request and User
validation rules. For answer fields, the OpenAPI document served by the local
baseline on port 8000 takes precedence: its `QueryResponse` has ten fields and
does not advertise the checkout's newer `context_status`. The response carries a fresh UUID, answer text,
token usage and first-turn defaults; the ID cannot yet be resumed. System and
ServiceAccount identities are not enabled for this console-only test service.

The [baseline notes](../tests/console-contract/README.md) record a 401 with a
string `detail` on a direct conversations probe, not an authenticated console
query. Neither this schema nor an offline model reply proves console parity.
Ticket 01 and a live synthetic Vertex smoke test remain cutover prerequisites.