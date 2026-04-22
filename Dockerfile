FROM registry.redhat.io/lightspeed-core/lightspeed-stack-rhel9:f271b085e4c77f2f9af92d43dc0a37afe7036748

USER root

# Install uv, then torch CPU-only first (avoids pulling ~6GB CUDA libs),
# then everything else from PyPI
RUN pip3.12 install "uv>=0.8.15" && \
    uv pip install --python /app-root/.venv/bin/python \
        --index-url https://download.pytorch.org/whl/cpu \
        torch && \
    uv pip install --python /app-root/.venv/bin/python \
        --index-url https://pypi.org/simple/ \
        app-common-python pyyaml \
        "sentence-transformers>=3.2.0" \
        "numpy>=2.0.0" \
        "psycopg2-binary>=2.9.0" \
        "pgvector>=0.3.0" \
        "mcp[cli]==1.26.0" \
        "httpx>=0.27.0"

# Pre-download the embedding model into the image (~420MB)
RUN python3.12 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-mpnet-base-v2')"

# OpenShift runs containers with a random UID in group 0 (root).
# Make /app-root writable by group 0 so the entrypoint can write rendered configs.
RUN chgrp -R 0 /app-root && chmod -R g=u /app-root

USER 1001

# Copy YAML templates, entrypoint, and reverse proxy
COPY run.yaml lightspeed-stack.yaml /app/
COPY entrypoint.py proxy.py /app/

# Copy embedding service and MCP discovery service source files
COPY embedding-service/main.py /app/embedding-service/main.py
COPY mcp-discovery-service/main.py mcp-discovery-service/mcp_server.py /app/mcp-discovery-service/

ENTRYPOINT ["python3.12", "/app/entrypoint.py"]
