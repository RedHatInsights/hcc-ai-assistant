# Console contract: partial baseline

No authenticated browser exchange has been captured. The only replayable evidence here is a direct, missing-identity HTTP probe. It is not proof the console calls this route.

## Observed on 2026-11-19

| Request through `127.0.0.1:8000` | Observation |
| --- | --- |
| `GET /readiness` | HTTP 200. The proxy source checks the backend readiness response. |
| `GET /api/ai-assistant/v1/conversations` | No identity or credentials, no request body. HTTP 401, `application/json`, `detail` string. No response values retained. |
| `GET /api/ai-assistant/openapi.json` | Advertises POST query and streaming_query under `/v1`, POST streaming_query/interrupt, and v1/v2 conversation list (GET) and item (GET, PUT, DELETE). Metadata only; none establishes console use. |

Port 8000 was published by `hcc-ai-assistant-lightspeed-stack`, image ID `sha256:01a9036283270cea7086565055a0548263d610d03289a7ae6abc5506777a024d`. Container revision label: `9b841548d63d7c7d3d4f1d722bb3868defaad1d4`. The label does **not** establish that the image was built from the ticket starting revision (`cfccd97b79c23cc70537ebf6ae82d8204bbdad33`). Identify the intended build before using these observations as a cutover gate.

With the local proxy already running, from the repository root run:

```sh
bash tests/console-contract/check-missing-identity.sh
```

Requires Bash, curl and jq. Exit zero means status, exact content type and scalar JSON field paths/types matched the fixture. The check sends only the fixed local GET, ignores curl startup configuration and proxies, follows no redirects and does not print or save the response. It does not test successful authentication or error wording.

## HCC client source evidence (not a browser capture)

The [HCC manager](https://github.com/RedHatInsights/astro-virtual-assistant-frontend/blob/b26b0484a75490f6982a7a286e626bc7bb57a9f5/src/aiClients/useHccAiManager.ts)
selects `/api/ai-assistant`, sets `streamMessages: false`, and supplies welcome buttons.
The [footer](https://github.com/RedHatInsights/astro-virtual-assistant-frontend/blob/b26b0484a75490f6982a7a286e626bc7bb57a9f5/src/Components/UniversalChatbot/UniversalFooter.tsx)
honors the manager flag for typed questions. The [welcome button handler](https://github.com/RedHatInsights/astro-virtual-assistant-frontend/blob/b26b0484a75490f6982a7a286e626bc7bb57a9f5/src/Components/UniversalChatbot/UniversalMessages.tsx)
explicitly sends `stream: true`. The HCC manager's fetch wrapper recognizes both routes;
that wrapper alone does not establish which is used.

The frontend lockfile selects `@redhat-cloud-services/ai-client-state` and
`@redhat-cloud-services/lightspeed-client` 0.15.0. The published state manager passes the
options through; the client POSTs `/v1/query` or `/v1/streaming_query` respectively,
with JSON `media_type`. Its JSON stream reader parses newline-terminated `data: ` records,
gets the conversation ID from `start`, appends `token` text, reads `end` metadata and
resolves at EOF. The [SDK integration fixture](https://github.com/RedHatInsights/ai-web-clients/blob/91db1c07dae9dc73806ddf6bab6113529237bc2e/apps/client-integration-tests/lightspeed-mock-server.js)
supplies the provisional success framing, field placement, ordering, and JSON content type
used in the replacement proxy test. It is not a deployed lcore response.

Exercise **both** welcome-button and typed submissions when collecting the live baseline.
Confirm the deployed frontend revision, MIME type, tool-related records, completion, and
safe failure before and during a stream. Do not infer these from other assistants or the
SDK's list of possible event types.

## Isolated build checks (2026-11-20)

The public frontend at `b26b0484` built with `npm ci --no-audit --no-fund` and
`npm run build` in a fresh UBI 10 Node 24 container (no host mounts or published
ports). Webpack emitted `fed-mods.json` and the `virtualAssistant` remote. This
is a **federated module**, not a bootstrapped browser session. The HCC manager
needs the `platform.chatbot.hcc-ai-assistant.enabled` flag and Chrome's
`auth.getToken()` for same-origin requests. The pinned dev config enables verbose
proxying and only adds local overrides for Rasa and MAS. Do not launch that
proxy against shared services for capture. Supply an isolated console host,
synthetic gateway identity mapping, and read-only downstream before replay.

The cached application image above and the cached `0.6-latest` base carry the
same revision label. The other local `lightspeed-ogx:local` image is labeled
`f6d6eb6f`; neither label identifies a build of `cfccd97`, whose Dockerfile
requires `lightspeed-stack-rhel9:0.7.3`. Pin the prior application image by digest
with source/build provenance before baseline or rollback testing.

### Replacement agent/proxy smoke only

From the repository root, with local Docker Desktop/Engine and Buildx:

```sh
bash tests/console-contract/smoke-candidate.sh
```

This builds the opt-in `agent-service/Dockerfile.candidate` from an allowlist,
using the agent lockfile and a digest-pinned Python/uv base, not lcore. The
build needs public dependency downloads. The run has only loopback, no host
mounts or published ports, a read-only image and temporary SQLite storage. It
starts the real agent factory on 8080 and existing proxy on 8000, checks ready
and live, verifies a missing-identity **query** returns JSON 401 with string
`detail`, then checks ready=503 and live=200 after the smoke agent exits. This
is not replay of the baseline **conversations** GET. It prints the local image
ID and leaves the stopped smoke container; it does not prune Docker resources.

No model call, identity, embedding/discovery process, or deployed console is
involved. This is not the full deployment image, a Vertex smoke, a four-service
startup check, or a rollback demonstration. The default build is unchanged.

## Collect the missing console evidence

1. Use the actual console client pointed at an identified lcore build. Record client revision and backend image ID. Use a disposable sandbox with synthetic resources and enforce read-only access at downstream systems. Do not rely on the prompt to prevent writes.
2. Use an approved observer that exposes only structural metadata. Do not export HAR, copy as curl, open raw headers/bodies, or enable request logging. Record header **names** and safe media types only. Never supply a token, encoded identity, real ID, or account-derived prose as a fixture. Distinguish a header seen on a success from one proven required by a rejection.
3. Start a fresh conversation. Ask a read-only question about synthetic resources, then a question that requires the first result. Confirm a downstream read actually executed (sanitized tool name or sandbox counter, not just a recommendation). Record which synthetic fact the follow-up must recover. At the observer, bind the first generated ID to a placeholder and record where the client reuses it (field, URL or header); do not retain its value.
4. Inventory actions from the console UI and its actual calls. For each, record action, method, public path template, request/response media types and field shapes, status, and any safe error structure. Record unexercised actions as unknown. Do not replay writes against shared services.
5. Inspect the client's response reader alongside observable framing. For a stream, record framing, ordered event types and payload fields, completion and an observed safe failure; ignore transport chunk boundaries. For a non-stream, record the media type and evidence that the client consumes one complete response. Proxy byte forwarding and route availability alone establish neither.
6. Replay those sanitized interactions **through port 8000** on the identified sandbox baseline. Substitute the first live ID into the follow-up at the observed location; assert field types, reference equality, synthetic result relationships and, when applicable, event order and completion. Compare neither generated IDs nor model wording literally. Mark each exchange observed, replayed, or still missing; never fill a gap with an invented response.

Still missing: client-used routes/actions, authenticated request and success shapes, tool execution and contextual follow-up, ID propagation, client stream consumption/completion/errors, and replay of those exchanges. Provide a console checkout and approved read-only sandbox, or an already sanitized structural trace with build provenance and tool-execution evidence. No credentials or raw capture are needed in this repository.