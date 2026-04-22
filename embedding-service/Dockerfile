FROM python:3.14-slim

WORKDIR /app

# Install torch CPU-only first to avoid pulling in ~6GB of CUDA libraries
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Install remaining dependencies from pyproject.toml
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Download the embedding model on build (caching)
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-mpnet-base-v2')"

# Copy application code
COPY main.py .

# Default host and port (can be overridden via env vars)
ENV HOST=0.0.0.0
ENV PORT=8002

EXPOSE ${PORT}

CMD sh -c "uvicorn main:app --host ${HOST} --port ${PORT}"
