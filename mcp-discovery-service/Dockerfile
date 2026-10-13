FROM python:3.15-slim

WORKDIR /app

# Install dependencies from pyproject.toml
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy application code
COPY main.py mcp_server.py .

# Create data directory
RUN mkdir -p /app-root/data

# Default host and port (can be overridden via env vars)
# HOST: 0.0.0.0 for IPv4, :: for IPv6/dual-stack
ENV HOST=0.0.0.0
ENV PORT=8001

# Expose MCP protocol port
EXPOSE ${PORT}

# Run the application
CMD ["python", "main.py"]
