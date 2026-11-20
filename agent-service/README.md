# Opt-in conversation test backend

The default deployment still runs lcore. This service is for a trusted, isolated
test deployment only. It trusts a gateway-supplied `x-rh-identity`; base64 is not
authentication. Do not expose it to untrusted callers or use real account data.
Use only synthetic console User identities. Both typed queries and the HCC
welcome-button JSON stream are supported in this test backend. The agent applies the configured
`pii-redaction` rules before classification and storage. Only an explicit
`ALLOWED` verdict from the tool-free `hcc-topic-guard` classifier starts an
answer turn. Rejected questions are not added to the conversation history.

For tool-backed turns, point `MCP_CONFIG_PATH` at the rendered stack YAML with
the discovery and downstream `mcp_servers` registrations. The agent uses their
Streamable HTTP URLs as configured. Each connection forwards only that server's
listed caller headers. A downstream call must match a recommendation from the
current turn and a tool still advertised by that server to this caller.

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
redacted question, conversation context, instructions, tool definitions and tool
results go to the answer model; caller headers are not model settings or prompts.
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
query. For welcome buttons the opt-in backend serves `POST /v1/streaming_query`
with the success records verified against frontend source and its SDK fixture:
`start`, incremental `token`, then `end` after persistence. A real loopback proxy
test checks delivery before model completion and a typed follow-up. Tool-event
reporting, error framing and deployed MIME/sequence parity remain unverified.
List, read, update and delete routes have not been added. Neither this schema
nor an offline model reply proves console parity.
Ticket 01 and a live synthetic Vertex smoke test remain cutover prerequisites.