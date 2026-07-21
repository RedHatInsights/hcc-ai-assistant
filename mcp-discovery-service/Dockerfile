FROM registry.access.redhat.com/hi/python:3.14-builder AS builder
USER 0
WORKDIR /app

# Create virtualenv for clean copy to runtime
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install dependencies from pyproject.toml
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Runtime stage — distroless (no shell)
FROM registry.access.redhat.com/hi/python:3.14

WORKDIR /app

# Copy virtualenv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code
COPY main.py mcp_server.py ./

# Create data directory for capabilities cache
USER 0
RUN ["python3", "-c", "import os; os.makedirs('/app-root/data', exist_ok=True); os.chmod('/app-root/data', 0o777)"]

# Default host and port (can be overridden via env vars)
ENV HOST=0.0.0.0
ENV PORT=8001

# Expose MCP protocol port
EXPOSE 8001

USER 1001

# Run the application
CMD ["python", "main.py"]
