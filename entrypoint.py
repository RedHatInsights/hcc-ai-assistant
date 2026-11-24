"""Entrypoint script that populates YAML config templates from environment
variables and Clowder ACG config, then starts the Pydantic AI agent behind a
reverse proxy that strips the /api/ai-assistant path prefix.

Also manages embedding service and MCP discovery service subprocesses."""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

import yaml

TEMPLATE_DIR = "/app"
RUNTIME_DIR = "/app-root"
STACK_YAML = "lightspeed-stack.yaml"


def load_clowder_config():
    """Load Clowder ACG config if CLOWDER_ENABLED is set."""
    if os.environ.get("CLOWDER_ENABLED", "").lower() not in ("true", "1", "yes"):
        print("[entrypoint] CLOWDER_ENABLED not set, skipping Clowder config")
        return None

    from app_common_python import LoadedConfig

    print("[entrypoint] Clowder config loaded")
    return LoadedConfig


def load_mcp_server_configs():
    """Load MCP server definitions from env var or local config file.

    Returns a dict keyed by server name. Each value contains provider_id, url,
    optional headers, and optional Clowder resolution fields (clowder_app,
    clowder_service, mcp_server_path).
    """
    raw = os.environ.get("CLOWDER_MCP_SERVER_CONFIGS")
    if not raw:
        config_path = os.path.join(TEMPLATE_DIR, "local_mcp_server_configs.json")
        try:
            with open(config_path) as f:
                raw = f.read()
            print(f"[entrypoint] Loaded MCP server configs from {config_path}")
        except FileNotFoundError:
            print("[entrypoint] No MCP server configs found, no dynamic MCP servers")
            return {}

    try:
        configs = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[entrypoint] ERROR: Malformed JSON in MCP server configs: {e}")
        return {}

    print(f"[entrypoint] Loaded MCP server configs: {len(configs)} server(s)")
    return configs


def merge_mcp_servers(stack_config, clowder):
    """Resolve the shared MCP server registrations for discovery and the agent."""
    mcp_configs = load_mcp_server_configs()
    if not mcp_configs:
        return

    mcp_servers = stack_config.setdefault("mcp_servers", [])

    existing_servers = {s["name"]: i for i, s in enumerate(mcp_servers)}

    for name, cfg in mcp_configs.items():
        provider_id = cfg["provider_id"]
        url = cfg.get("url", "")

        if not url and not cfg.get("clowder_app"):
            print(f"[entrypoint] ERROR: MCP server {name} has no 'url' and no 'clowder_app' — skipping")
            continue

        server_entry = {"name": name, "provider_id": provider_id, "url": url}
        if "headers" in cfg:
            server_entry["headers"] = cfg["headers"]

        if name in existing_servers:
            mcp_servers[existing_servers[name]] = server_entry
        else:
            mcp_servers.append(server_entry)

        print(f"[entrypoint] Registered MCP server {name} ({url})")

    if not clowder or not getattr(clowder, "endpoints", None):
        return

    server_by_name = {s["name"]: s for s in mcp_servers}

    for name, cfg in mcp_configs.items():
        clowder_app = cfg.get("clowder_app")
        if not clowder_app:
            continue

        service_name = cfg.get("clowder_service")
        mcp_server_path = cfg.get("mcp_server_path", "/")

        endpoint = None
        for ep in clowder.endpoints:
            if ep.app == clowder_app:
                if service_name and ep.name != service_name:
                    continue
                endpoint = ep
                break

        if not endpoint:
            print(f"[entrypoint] No Clowder endpoint for {name} (app={clowder_app}), keeping URL")
            continue

        resolved_url = f"http://{endpoint.hostname}:{endpoint.port}{mcp_server_path}"

        server = server_by_name.get(name)
        if server:
            server["url"] = resolved_url

        print(f"[entrypoint] Resolved {name} -> {resolved_url}")


def render_configs(clowder):
    """Read template YAMLs, apply Clowder config, write to runtime dir.

    Returns the rendered stack config so callers can read values from it.
    """
    os.makedirs(RUNTIME_DIR, exist_ok=True)

    stack_template = os.path.join(TEMPLATE_DIR, STACK_YAML)

    with open(stack_template) as f:
        stack_config = yaml.safe_load(f)

    merge_mcp_servers(stack_config, clowder)

    stack_out = os.path.join(RUNTIME_DIR, STACK_YAML)

    with open(stack_out, "w") as f:
        yaml.dump(stack_config, f, default_flow_style=False, sort_keys=False)

    print(f"[entrypoint] Wrote {stack_out}")

    return stack_config


def set_db_env_vars(clowder):
    """Export DB connection info as PG* env vars for the embedding service."""
    if clowder is None or not clowder.database:
        print("[entrypoint] No Clowder DB config, embedding service will use defaults")
        return

    db = clowder.database
    ssl_mode = getattr(db, "sslMode", None) or "prefer"

    # TODO: Move env var plumbing to a config/env helper
    os.environ.setdefault("PGHOST", db.hostname)
    os.environ.setdefault("PGPORT", str(db.port))
    os.environ.setdefault("PGDATABASE", db.name)
    os.environ.setdefault("PGUSER", db.username)
    os.environ.setdefault("PGPASSWORD", db.password)
    os.environ.setdefault("PGSSLMODE", ssl_mode)

    # The embedding service uses psycopg2 which reads PGSSLROOTCERT for the
    # CA cert path when sslmode=verify-full.
    rds_ca = getattr(db, "rdsCa", None)
    if rds_ca:
        with open("/tmp/rds-ca.crt", "w") as cert:
            cert.write(rds_ca)
        os.environ.setdefault("PGSSLROOTCERT", "/tmp/rds-ca.crt")

    print(f"[entrypoint] Set PG* env vars for embedding service ({db.hostname}:{db.port}/{db.name})")


def wait_for_health(url, timeout, name):
    """Poll a health endpoint until it returns 200 or timeout is reached."""
    print(f"[entrypoint] Waiting for {name} at {url} (timeout={timeout}s)...")
    deadline = time.monotonic() + timeout
    interval = 2

    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    print(f"[entrypoint] {name} is healthy")
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(interval)

    print(f"[entrypoint] WARNING: {name} did not become healthy within {timeout}s, continuing anyway")


def main():
    clowder = load_clowder_config()
    stack_config = render_configs(clowder)

    # Export DB config as env vars for the embedding service subprocess
    set_db_env_vars(clowder)

    # Track all child processes for cleanup
    children = []

    # --- Start embedding service ---
    embedding_port = os.environ.get("EMBEDDING_SERVICE_PORT", "8002")
    print(f"[entrypoint] Starting embedding service on port {embedding_port}...")
    embedding_proc = subprocess.Popen([
        "python3.12", "-m", "uvicorn", "main:app",
        "--host", "0.0.0.0",
        "--port", embedding_port,
        "--app-dir", "/app/embedding-service",
    ])
    children.append(("embedding-service", embedding_proc))
    wait_for_health(f"http://localhost:{embedding_port}/health", timeout=120, name="embedding-service")

    # --- Start MCP discovery service ---
    mcp_discovery_port = os.environ.get("MCP_DISCOVERY_SERVICE_PORT", "8001")
    # TODO: Move env var plumbing to a config/env helper
    os.environ.setdefault("ENABLE_VECTOR_STORE", "true")
    os.environ.setdefault("EMBEDDING_SERVICE_URL", f"http://localhost:{embedding_port}")
    os.environ.setdefault("MCP_CONFIG_PATH", os.path.join(RUNTIME_DIR, STACK_YAML))
    os.environ.setdefault("CAPABILITIES_CACHE_PATH", os.path.join(RUNTIME_DIR, "data", "mcp-capabilities.json"))
    os.environ.setdefault("HOST", "0.0.0.0")
    os.environ.setdefault("PORT", mcp_discovery_port)

    print(f"[entrypoint] Starting MCP discovery service on port {mcp_discovery_port}...")
    mcp_discovery_proc = subprocess.Popen([
        "python3.12", "/app/mcp-discovery-service/main.py",
    ])
    children.append(("mcp-discovery-service", mcp_discovery_proc))
    wait_for_health(f"http://localhost:{mcp_discovery_port}/health", timeout=60, name="mcp-discovery-service")

    service_config = stack_config.get("service", {})
    backend_host = "127.0.0.1"
    backend_port = service_config.get("port", 8080)

    backend_url = f"http://{backend_host}:{backend_port}"
    # TODO: Move env var plumbing to a config/env helper
    os.environ.setdefault("PROXY_BACKEND_URL", backend_url)

    os.environ.setdefault("AGENT_SQLITE_PATH", os.path.join(RUNTIME_DIR, "agent.sqlite"))
    print(f"[entrypoint] Starting Pydantic AI agent on {backend_host}:{backend_port}...")
    backend = subprocess.Popen([
        "/app/agent-service/.venv/bin/python", "-m", "uvicorn", "turn:create_app", "--factory",
        "--app-dir", "/app/agent-service", "--host", backend_host, "--port", str(backend_port),
    ])
    children.append(("agent", backend))

    # Forward signals to all child processes
    def handle_signal(signum, _frame):
        for name, proc in children:
            print(f"[entrypoint] Terminating {name} (pid={proc.pid})...")
            proc.terminate()
        for name, proc in children:
            proc.wait()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # --- Run the reverse proxy (blocks until shutdown) ---
    proxy_host = os.environ.get("PROXY_HOST", "0.0.0.0")
    proxy_port = int(os.environ.get("PROXY_PORT", "8000"))
    proxy_log_level = os.environ.get("PROXY_LOG_LEVEL", "warning")
    print(f"[entrypoint] Starting reverse proxy on {proxy_host}:{proxy_port} (log_level={proxy_log_level})...")
    import uvicorn

    try:
        uvicorn.run("proxy:app", host=proxy_host, port=proxy_port,
                     log_level=proxy_log_level, app_dir="/app")
    finally:
        for name, proc in children:
            print(f"[entrypoint] Terminating {name} (pid={proc.pid})...")
            proc.terminate()
            proc.wait()


if __name__ == "__main__":
    main()
