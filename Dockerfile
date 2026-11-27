FROM registry.access.redhat.com/ubi9/python-312@sha256:5a0471c98fc10017d011c16b7615cd1d7f157d796f89eb4900ca0dc6c87384cf AS agent-deps
USER root
RUN /usr/bin/python3.12 -m pip install --no-cache-dir --no-deps --only-binary=:all: \
    --index-url https://pypi.org/simple/ uv==0.9.30
ARG UV_CACHE_DIR=/root/.cache/uv

WORKDIR /app/agent-service
COPY agent-service/pyproject.toml agent-service/uv.lock ./
RUN --mount=type=cache,id=hcc-agent-uv,target=/root/.cache/uv \
    UV_HTTP_TIMEOUT=120 UV_CONCURRENT_DOWNLOADS=4 UV_LINK_MODE=copy \
    uv sync --python /usr/bin/python3.12 --locked --no-dev --no-install-project --no-python-downloads

FROM agent-deps AS runtime
COPY agent-service/turn.py agent-service/conversations.py agent-service/discovered_tools.py agent-service/agent_card.py ./

WORKDIR /app
ARG UV_HTTP_TIMEOUT=120
ARG UV_CONCURRENT_DOWNLOADS=4
ARG UV_LINK_MODE=copy
RUN uv venv --python /usr/bin/python3.12 --no-python-downloads /opt/hcc-services
ENV PATH="/opt/hcc-services/bin:${PATH}"
RUN --mount=type=cache,id=hcc-system-uv,target=/root/.cache/uv \
    uv pip install --python /opt/hcc-services/bin/python \
    --index-url https://download.pytorch.org/whl/cpu \
    torch
RUN --mount=type=cache,id=hcc-system-uv,target=/root/.cache/uv \
    uv pip install --python /opt/hcc-services/bin/python \
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
