FROM registry.access.redhat.com/hi/python:3.14-builder AS builder
USER 0
WORKDIR /app

# Create virtualenv for clean copy to runtime
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install torch CPU-only first to avoid pulling in ~6GB of CUDA libraries
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Install remaining dependencies from pyproject.toml
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Download the embedding model on build (caching)
ENV HF_HOME=/opt/hf_cache
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-mpnet-base-v2')"

# Runtime stage — distroless (no shell)
FROM registry.access.redhat.com/hi/python:3.14

WORKDIR /app

# Copy virtualenv and model cache from builder
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/hf_cache /opt/hf_cache

ENV PATH="/opt/venv/bin:$PATH"
ENV HF_HOME=/opt/hf_cache
# Never reach out to HuggingFace at runtime — use only the pre-baked model
ENV TRANSFORMERS_OFFLINE=1

# Copy application code
COPY main.py .

# Default host and port (can be overridden via env vars)
ENV HOST=0.0.0.0
ENV PORT=8002

EXPOSE 8002

USER 1001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]
