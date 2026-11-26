@AGENTS.md

## Commands

```bash
# Run tests
cd embedding-service && pip install -e ".[dev]" && pytest -v
cd mcp-discovery-service && pip install -e ".[dev]" && pytest -v
pytest -v  # Root-level tests (entrypoint + migrations)

# Run tests with coverage
cd embedding-service && pytest --cov=main --cov-report=html
cd mcp-discovery-service && pytest --cov=main --cov=mcp_server --cov-report=html

# Format code
black .

# Lint
ruff check .
ruff check --fix .

# Start all services locally (Docker Compose)
docker compose up -d --build

# Health checks
curl http://localhost:8000/liveness   # Proxy liveness
curl http://localhost:8000/readiness  # Proxy readiness (checks backend)
curl http://localhost:8001/health     # MCP Discovery
curl http://localhost:8002/health     # Embedding Service
```

## Git Conventions

- Branch format: `bot/<TICKET-KEY>`, `feature/<description>`, or `<username>/<description>`
- Commit format: `type(scope): description` (conventional commits)
- Scopes: `proxy`, `embedding`, `embedding-service`, `mcp-discovery`, `config`, `docker`, `ci`, `pipelines`, `tekton`
- Types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`

## Key Files

- `entrypoint.py` - Main orchestrator (starts all services)
- `proxy.py` - Reverse proxy (strips /api/ai-assistant prefix)
- `lightspeed-stack.yaml` - LightSpeed Core config (MCP servers, auth, prompts)
- `run.yaml` - llama-stack config (providers, storage, models)
- `config/clowdapp.yaml` - OpenShift deployment template
- `migrations.py` - Database migrations (runs on startup)
- `embedding-service/main.py` - Embedding + vector storage API
- `mcp-discovery-service/main.py` - MCP tool indexer
- `mcp-discovery-service/mcp_server.py` - MCP protocol server (4 tools)

## Agent skills

### Issue tracker

Read and write issues as local Markdown under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the five default triage labels in each issue's `Status:` line. See `docs/agents/triage-labels.md`.

### Domain docs

Use the single-context root glossary and shared ADRs when exploring the codebase. See `docs/agents/domain.md`.
