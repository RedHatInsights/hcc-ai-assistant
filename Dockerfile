FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58

USER root

WORKDIR /app/agent-service
COPY agent-service/pyproject.toml agent-service/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache --no-python-downloads
COPY agent-service/turn.py agent-service/conversations.py agent-service/discovered_tools.py ./

WORKDIR /app
RUN uv pip install --system \
        --index-url https://download.pytorch.org/whl/cpu \
        torch && \
    uv pip install --system \
        --index-url https://pypi.org/simple/ \
        app-common-python pyyaml fastapi uvicorn \
        "sentence-transformers>=3.2.0" \
        "numpy>=2.0.0" \
        "psycopg2-binary>=2.9.0" \
        "pgvector>=0.3.0" \
        "mcp[cli]==1.26.0" \
        "httpx>=0.27.0"

# Pre-download the embedding model into the image (~420MB).
# HF_HOME must be set before download so the cache lands in a group-writable
# location accessible by OpenShift's random UID.
ENV HF_HOME=/app-root/hf_cache
RUN python3.12 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-mpnet-base-v2')"

# Never reach out to HuggingFace at runtime — use only the pre-baked model.
ENV TRANSFORMERS_OFFLINE=1

# OpenShift runs containers with a random UID in group 0 (root).
# Make /app-root writable by group 0 so the entrypoint can write rendered configs.
RUN mkdir -p /app-root/data && chgrp -R 0 /app-root && chmod -R g=u /app-root

USER 1001

# Copy YAML templates, entrypoint, and reverse proxy
COPY lightspeed-stack.yaml entrypoint.py proxy.py /app/

# Copy embedding service and MCP discovery service source files
COPY embedding-service/main.py /app/embedding-service/main.py
COPY mcp-discovery-service/main.py mcp-discovery-service/mcp_server.py /app/mcp-discovery-service/

ENTRYPOINT ["python3.12", "/app/entrypoint.py"]
