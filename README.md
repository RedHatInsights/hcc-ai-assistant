# AI Assistant Service

HTTP-based AI assistant service using LightSpeed stack with Google Vertex AI and MCP (Model Context Protocol) discovery capabilities.

## Overview

This project provides a production-ready AI assistant with:
- **Google Vertex AI** integration via LightSpeed Core
- **MCP Tool Discovery** - automatically discovers and indexes available MCP tools
- **Vector Embeddings** - semantic search powered by sentence-transformers + ChromaDB
- **Sidecar Architecture** - all services run in a single pod, communicating via localhost

All services are built with **Python** for a unified development experience.

## Architecture

### Kubernetes/OpenShift (Sidecar Deployment)

In production, all services run in a single pod as sidecars, communicating via `localhost`:

```
┌────────────────────────────────────────────────┐
│ Pod: hcc-ai-assistant                          │
│                                                │
│  ┌──────────────────────┐                     │
│  │ Container: api       │  port 8000          │
│  │ (lightspeed-stack)   │◄────────────────────┤─ Public
│  │ - Google Vertex AI   │                     │
│  │ - RAG capabilities   │                     │
│  │ - Conversation cache │                     │
│  └───────┬──────────────┘                     │
│          │ localhost:8001/mcp                 │
│          ▼                                     │
│  ┌──────────────────────┐                     │
│  │ Sidecar: mcp-disc    │  port 8001          │
│  │ (Python/FastMCP)     │                     │
│  │ - MCP Server         │                     │
│  │ - 4 MCP Tools        │                     │
│  │ - Tool Discovery     │                     │
│  └───────┬──────────────┘                     │
│          │ localhost:8002                     │
│          ▼                                     │
│  ┌──────────────────────┐                     │
│  │ Sidecar: embedding   │  port 8002          │
│  │ (Python)             │                     │
│  │ - Embeddings         │                     │
│  │ - Vector Store       │                     │
│  │ - ChromaDB           │                     │
│  └──────────────────────┘                     │
└────────────────────────────────────────────────┘
```

**Startup Order**: embedding-service → mcp-discovery-service → lightspeed-stack

## Quick Start

### Docker Compose (Recommended for Local Development)

```bash
# Copy environment template
cp .env.example .env

# Edit .env with your values:
# VERTEX_PROJECT=your-project-id
# VERTEX_LOCATION=your-location
# ALLOWED_MODEL=your-model-name
# GOOGLE_API_KEY=your-api-key

# Start all services
docker compose up -d --build

# Check health of all services
curl http://localhost:8000/health  # LightSpeed Core
curl http://localhost:8001/health  # MCP Discovery
curl http://localhost:8002/health  # Embedding Service
```

Services will be available at:
- **LightSpeed Stack**: http://localhost:8000
- **MCP Discovery**: http://localhost:8001
- **Embedding Service**: http://localhost:8002

**Note**: Docker Compose simulates the Kubernetes sidecar architecture using `network_mode: "service:lightspeed-stack"`. All services share the same network namespace and communicate via `localhost`.

## Services

### 1. LightSpeed Stack (Port 8000)
Main AI assistant service powered by Google Vertex AI.

**Key Features:**
- Google Vertex AI integration (Gemini models)
- RAG (Retrieval Augmented Generation)
- Conversation caching
- MCP tool discovery integration

### 2. MCP Discovery Service (Port 8001)
Discovers and indexes MCP tools from configured servers, exposing them via the MCP protocol.

**Key Features:**
- Full MCP protocol server (FastMCP SDK)
- 4 MCP tools: search, list, get_schema, recommend
- Vector-based semantic search + keyword fallback
- Background refresh every 5 minutes

📖 **[Full Documentation](./mcp-discovery-service/README.md)**

### 3. Embedding Service (Port 8002)
Unified service for text embeddings and vector storage.

**Key Features:**
- Text embeddings via sentence-transformers
- Vector storage via ChromaDB
- llama-stack compatible API
- Fast: ~10-50ms embeddings, ~5-20ms queries

📖 **[Full Documentation](./embedding-service/README.md)**

## Configuration

### Required Environment Variables

Create a `.env` file with:

```bash
# Google Cloud / Vertex AI
VERTEX_PROJECT=your-project-id
VERTEX_LOCATION=your-location
ALLOWED_MODEL=gemini-2.5-flash
GOOGLE_API_KEY=your-api-key

# Optional: Enable vector embeddings (enabled by default in docker-compose)
ENABLE_VECTOR_STORE=true
```

For detailed configuration options, see individual service READMEs:
- [MCP Discovery Configuration](./mcp-discovery-service/README.md#configuration)
- [Embedding Service Configuration](./embedding-service/README.md#configuration)

### MCP Server Configuration

MCP servers to discover are configured in `lightspeed-stack.yaml` (the `mcp_servers` section):

```yaml
mcp_servers:
  - name: "my-mcp-server"
    provider_id: "my-mcp-provider"
    url: "http://my-mcp-server:3001/mcp"
```

In Kubernetes/OpenShift, this is managed via ConfigMap (mounted as lightspeed-stack.yaml).

### Adding MCP Servers Locally

To register MCP servers for local development, edit `local_mcp_server_configs.json` in the project root:

```json
{
  "my-mcp-server": {
    "provider_id": "my-mcp-provider",
    "url": "http://localhost:9000/mcp/",
    "headers": ["x-rh-identity"]
  }
}
```

Each entry requires:
- **key**: A unique server name (e.g. `"my-mcp-server"`)
- **`provider_id`**: Unique provider identifier used in the LightSpeed stack tool runtime
- **`url`**: The MCP server endpoint URL

Optional fields:
- **`headers`**: List of HTTP headers to forward to the MCP server
- **`clowder_app`**: Clowder application name for automatic URL resolution in-cluster
- **`clowder_service`**: Clowder service name (used with `clowder_app` to match the correct endpoint)
- **`mcp_server_path`**: Path appended to the resolved Clowder URL (default: `"/"`)

The entrypoint loads this file as a fallback when the `CLOWDER_MCP_SERVER_CONFIGS` environment variable is not set. In deployed environments, the env var is populated from a ConfigMap via app-interface.

## Testing

### Quick Health Checks

```bash
# Check all services
curl http://localhost:8000/health  # LightSpeed Core
curl http://localhost:8001/health  # MCP Discovery
curl http://localhost:8002/health  # Embedding Service
```

### Test the AI Assistant

```bash
curl -X POST http://localhost:8000/v1/query \
  -H "Content-Type: application/json" \
  -H "x-rh-identity: test" \
  -d '{
    "query": "What MCP tools are available?",
    "provider": "google-vertex",
    "model": "google-vertex/google/gemini-2.5-flash"
  }'
```

The LLM will automatically discover and use MCP tools to answer questions.

### Running Unit Tests

Both Python services include comprehensive test suites:

```bash
# Embedding Service (19 tests)
cd embedding-service
pip install -e ".[dev]"
pytest -v

# MCP Discovery Service (53 tests)
cd mcp-discovery-service
pip install -e ".[dev]"
pytest -v
```

See individual service READMEs for detailed testing documentation.

## Development

### Local Development Setup

Each service can be run independently for development:

```bash
# Embedding Service
cd embedding-service
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn main:app --reload --port 8002

# MCP Discovery Service
cd mcp-discovery-service
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python main.py
```

See individual service READMEs for detailed development instructions:
- [Embedding Service Development](./embedding-service/README.md#development)
- [MCP Discovery Service Development](./mcp-discovery-service/README.md#development)

## Deployment

### Docker Compose (Local)

```bash
docker-compose up -d --build    # Start all services
docker-compose logs -f          # View logs
docker-compose down             # Stop services
```

### OpenShift/Kubernetes

See [config/clowdapp.yaml](./config/clowdapp.yaml) for deployment configuration.

All three services run as containers in a single pod:
- **lightspeed-stack** (main container) - Port 8000 exposed publicly
- **mcp-discovery-service** (sidecar) - Port 8001 internal
- **embedding-service** (sidecar) - Port 8002 internal

```bash
# Deploy to OpenShift
oc process -f config/clowdapp.yaml \
  -p ENV_NAME=your-env \
  -p IMAGE_TAG=latest \
  | oc apply -f -

# Check status
oc get pods
oc logs -f <pod-name> -c mcp-discovery-service
oc logs -f <pod-name> -c embedding-service
```

## Troubleshooting

### Services Won't Start

**Check logs for each service:**
```bash
docker-compose logs lightspeed-stack
docker-compose logs mcp-discovery-service
docker-compose logs embedding-service
```

### Vector Embeddings Not Working

**Symptoms:** MCP discovery logs show "falling back to keyword search"

**Quick checks:**
```bash
# Verify embedding service is healthy
curl http://localhost:8002/health

# Check if MCP discovery can reach it (via localhost in sidecar)
docker-compose exec mcp-discovery-service curl http://localhost:8002/health

# Verify environment variable
docker-compose exec mcp-discovery-service env | grep ENABLE_VECTOR_STORE
```

**See detailed troubleshooting:**
- [MCP Discovery Troubleshooting](./mcp-discovery-service/README.md#troubleshooting)
- [Embedding Service Troubleshooting](./embedding-service/README.md#troubleshooting)

### Out of Memory

Increase memory limits in docker-compose.yml or your Kubernetes deployment.

**Minimum requirements:**
- Embedding Service: 1GB RAM
- MCP Discovery: 256MB RAM

See [MCP Discovery Resources](./mcp-discovery-service/README.md#resource-requirements) and [Embedding Service Resources](./embedding-service/README.md#resource-requirements) for details.

## Why Python?

All services use Python for:
- ✅ **Unified language stack** - one language, one set of tools
- ✅ **Simplified CI/CD** - single build pipeline
- ✅ **Easier maintenance** - consistent patterns across services
- ✅ **Team efficiency** - single skill set required
- ✅ **ML/AI ecosystem** - natural fit for embedding and AI work

## Why Sidecar Architecture?

Services run as sidecars in a single pod for:
- ✅ **Low latency** - `localhost` communication is faster than network calls
- ✅ **Simplified networking** - no service discovery needed
- ✅ **Resource efficiency** - shared network namespace
- ✅ **Atomic deployments** - all services deploy together
- ✅ **Guaranteed startup order** - enforced by probes

## Contributing

### Code Style

Both Python services use:
- **Black** for formatting
- **Ruff** for linting
- **pytest** for testing

```bash
# Format code
black .

# Lint
ruff check .

# Run tests
pytest -v
```

### Pull Requests

1. Create a feature branch
2. Add tests for new functionality
3. Ensure all tests pass (`pytest`)
4. Format code (`black .`)
5. Update relevant READMEs

## License

See LICENSE file for details.
