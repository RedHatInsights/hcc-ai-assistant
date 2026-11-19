# Opt-in conversation test backend

The default deployment still runs lcore. This service is for a trusted, isolated
test deployment only. It trusts a gateway-supplied `x-rh-identity`; base64 is not
authentication. Do not expose it to untrusted callers or use real account data.
Only synthetic console User identities and synchronous turns are in scope.
There is no tool access or streaming. The agent applies the configured
`pii-redaction` rules before classification and storage. Only an explicit
`ALLOWED` verdict from the tool-free `hcc-topic-guard` classifier starts an
answer turn. Rejected questions are not added to the conversation history.

From the repository root, run the offline proxy tests:

```sh
uv run --project agent-service --locked pytest -q agent-service/test_turn.py
```

The PostgreSQL test starts and stops a disposable, Unix-socket-only cluster when
`initdb` and `pg_ctl` are on `PATH` (run as a non-root user); otherwise it skips.
The other tests use temporary SQLite files.

For an approved Vertex test, set `VERTEX_PROJECT`, `VERTEX_LOCATION`, and
`ALLOWED_MODEL` to the test Gemini deployment. Configure Application Default
Credentials (for example via `GOOGLE_APPLICATION_CREDENTIALS`); do not include
credentials in requests. `MCP_CONFIG_PATH` optionally selects a rendered stack
YAML; otherwise the agent reads the assistant instructions and shields from
the repository template. In separate terminals, on a host with both ports available:

```sh
mkdir -p -m 700 "$HOME/.local/share/hcc-agent-test"
export AGENT_SQLITE_PATH="$HOME/.local/share/hcc-agent-test/chat.sqlite"
uv run --project agent-service --locked uvicorn turn:create_app --factory --app-dir agent-service --host 127.0.0.1 --port 8080
PROXY_BACKEND_URL=http://127.0.0.1:8080 uv run --project agent-service --locked uvicorn proxy:app --host 127.0.0.1 --port 8000
```

Use `POST /api/ai-assistant/v1/query` with a JSON `query` and a trusted test
identity with `org_id`, `user.user_id`, and `user.username`. Omit `conversation_id`
to start a chat; send the returned ID on the same route to continue. Only the
question, prior model messages, and configured instructions go to the model.
`GET /readiness` means the proxy can reach the initialized agent, not that Vertex
has accepted credentials or has quota. `GET /liveness` is independent of the agent.

When `PGHOST` is set, the agent uses PostgreSQL with the deployment's existing
libpq `PG*` settings (including TLS), even if `AGENT_SQLITE_PATH` is set. A failed
connection does not fall back to SQLite. Initialization only creates the new
`agent_conversations_v1` table if absent; no legacy or embedding data is migrated.
Without `PGHOST`, set an explicit SQLite file in a private directory. Both stores
contain message history, not just IDs; use synthetic data in this test backend.

## Provisional contract

Local lightspeed-stack revision `634d32eb9affba8b7022ed4dd8efc8bd0fac345a`:
`docs/devel_doc/openapi.json`, `src/models/api/responses/successful/query.py`,
and `src/authentication/rh_identity.py` supply the synchronous request and User
validation rules. For answer fields, the OpenAPI document served by the local
baseline on port 8000 takes precedence: its `QueryResponse` has ten fields and
does not advertise the checkout's newer `context_status`. The response carries a fresh UUID, answer text,
token usage and first-turn defaults; follow-ups retain the ID. System and
ServiceAccount identities are not enabled for this console-only test service.

The [baseline notes](../tests/console-contract/README.md) record a 401 with a
string `detail` on a direct conversations probe, not an authenticated console
query. No authenticated capture establishes list, read, update, delete, or
streaming behavior; none of those routes has been added. Neither this schema
nor an offline model reply proves console parity.
Ticket 01 and a live synthetic Vertex smoke test remain cutover prerequisites.