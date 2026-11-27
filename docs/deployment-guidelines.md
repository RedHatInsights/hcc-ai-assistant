# Deployment Guidelines

## Local Development

### Docker Compose (Recommended)

```bash
# Copy environment template
cp .env.example .env
# Edit .env with your Google Cloud / Vertex AI credentials

# Start all services
docker compose up -d --build

# Verify health
curl http://localhost:8000/liveness   # Proxy liveness
curl http://localhost:8000/readiness  # Proxy readiness (checks backend)
curl http://localhost:8001/health  # MCP Discovery
curl http://localhost:8002/health  # Embedding Service

# View logs
docker compose logs -f
docker compose logs -f lightspeed-stack  # Single service

# Stop
docker compose down
```

Docker Compose simulates the production architecture:
- PostgreSQL (pgvector) runs as a separate service
- All application services run as subprocesses inside the `lightspeed-stack` container (same as production)
- Services communicate via `localhost`

### Individual Service Development

For rapid iteration on a single service:

```bash
# Embedding Service
cd embedding-service
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
# Requires a running PostgreSQL with pgvector extension
uvicorn main:app --reload --port 8002

# MCP Discovery Service
cd mcp-discovery-service
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python main.py
```

## OpenShift / Kubernetes Deployment

### ClowdApp Configuration

The deployment is managed via `config/clowdapp.yaml`:

```bash
oc process -f config/clowdapp.yaml \
  -p ENV_NAME=your-env \
  -p IMAGE_TAG=latest \
  -p VERTEX_PROJECT=your-project \
  -p VERTEX_LOCATION=us-central1 \
  -p ALLOWED_MODEL=gemini-2.5-flash \
  | oc apply -f -
```

### A2A gateway configuration

Leave the optional `A2A_PUBLIC_URL` template parameter empty to keep discovery and invocation disabled.
For stage, set it to `https://console.stage.redhat.com/api/ai-assistant/a2a` only after the gateway
route and end-user identity policy are confirmed. This is the public invocation URL, not the card URL
or a credential. Setting it does not create a gateway route or change the console query endpoints.

After rollout, fetch `GET /api/ai-assistant/.well-known/agent-card.json` through the gateway and check
that the JSON-RPC 1.0 interface advertises the configured URL. Use an approved test user's bearer token
to send `SendMessage` there, then send a follow-up with the returned `contextId`. Verify invocation
rejects missing or invalid credentials and that the existing `POST /api/ai-assistant/v1/query` still works.

### Resource Requirements

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-----------|----------|---------------|-------------|
| Pod (total) | 1 core | 2 cores | 4Gi | 6Gi |
| Embedding model | - | - | ~1.5Gi | - |
| MCP Discovery | - | - | ~256Mi | - |
| LightSpeed Stack | - | - | ~1Gi | - |

### Clowder Integration

When `CLOWDER_ENABLED=true`:
1. **Database**: Clowder provisions a PostgreSQL instance. The entrypoint switches all storage backends from SQLite to PostgreSQL automatically
2. **Feature Flags**: Enabled via Clowder's Unleash integration
3. **Kafka**: Topic `platform.chrome` provisioned (8 partitions, 1 replica)
4. **Secrets**: Vertex AI credentials mounted from Kubernetes Secret at `/opt/secrets`

### Health Probes

| Probe | Path | Port | Initial Delay | Period |
|-------|------|------|--------------|--------|
| Startup | `/readiness` | 8000 | 10s | 10s |
| Liveness | `/liveness` | 8000 | 5s | 10s |
| Readiness | `/readiness` | 8000 | 5s | 10s |

Both liveness and readiness probes hit the reverse proxy. `/readiness` forwards to the LightSpeed backend; `/liveness` responds directly from the proxy.

## Container Image

### Base Image

```text
registry.redhat.io/lightspeed-core/lightspeed-stack-rhel9:<sha>
```

The base image includes LightSpeed Core (llama-stack wrapper) with Python 3.12.

### Build Layers

1. **Base**: LightSpeed Stack RHEL9
2. **Dependencies**: uv, torch (CPU-only), sentence-transformers, numpy, psycopg2, pgvector, mcp SDK, httpx
3. **Embedding model**: Pre-downloaded `all-mpnet-base-v2` (~420MB) into `/app-root/hf_cache`
4. **Application**: YAML configs, entrypoint, proxy, service source files

### Key Environment Variables in Image

```text
HF_HOME=/app-root/hf_cache          # Pre-baked model cache
TRANSFORMERS_OFFLINE=1               # Never download models at runtime
```

### OpenShift UID Handling

OpenShift runs containers with a random UID in group 0 (root). The Dockerfile ensures `/app-root` is group-writable:

```dockerfile
RUN chgrp -R 0 /app-root && chmod -R g=u /app-root
USER 1001
```

## CI/CD

### Tekton Pipelines (Konflux)

- `.tekton/hcc-ai-assistant-pull-request.yaml` - PR validation pipeline
- `.tekton/hcc-ai-assistant-push.yaml` - Push/merge pipeline

### Building Locally

```bash
# Build the full image
docker build -t hcc-ai-assistant:dev .

# Build takes ~10 minutes (torch download + model pre-bake)
# Subsequent builds use Docker cache
```

## Troubleshooting

### Services Won't Start

```bash
# All service logs are in the single lightspeed-stack container
docker compose logs lightspeed-stack

# In OpenShift (single container, all services log together)
oc logs -f <pod> -c api
```

### Vector Search Falling Back to Keywords

```bash
# Verify embedding service is healthy
curl http://localhost:8002/health

# Check ENABLE_VECTOR_STORE is set
docker compose exec lightspeed-stack env | grep VECTOR

# Check embedding service connectivity from MCP discovery
docker compose exec lightspeed-stack curl http://localhost:8002/health
```

### Out of Memory

- Embedding model requires ~1.5GB RAM minimum
- Increase pod memory limits in `clowdapp.yaml`
- Check for memory leaks in vector store queries (large result sets)

### Database Connection Issues

```bash
# Verify PostgreSQL is running
docker compose exec postgres pg_isready -U postgres

# Check pgvector extension
docker compose exec postgres psql -U postgres -d hcc-ai-assistant -c "SELECT * FROM pg_extension WHERE extname = 'vector';"

# Check PG* environment variables
docker compose exec lightspeed-stack env | grep PG
```
