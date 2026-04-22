"""Entrypoint script that populates YAML config templates from environment
variables and Clowder ACG config, then starts lightspeed-stack behind a
reverse proxy that strips the /api/ai-assistant path prefix.

Also manages embedding service and MCP discovery service subprocesses."""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error

import yaml

TEMPLATE_DIR = "/app"
RUNTIME_DIR = "/app-root"
RUN_YAML = "run.yaml"
STACK_YAML = "lightspeed-stack.yaml"


def load_clowder_config():
    """Load Clowder ACG config if CLOWDER_ENABLED is set."""
    if not os.environ.get("CLOWDER_ENABLED", "").lower() in ("true", "1", "yes"):
        print("[entrypoint] CLOWDER_ENABLED not set, skipping Clowder config")
        return None

    from app_common_python import LoadedConfig

    print("[entrypoint] Clowder config loaded")
    return LoadedConfig


def load_clowder_mcp_config():
    """Load MCP server-to-Clowder-endpoint mapping from env var (injected via ConfigMap)."""
    raw = os.environ.get("CLOWDER_MCP_SERVER_CONFIGS")
    if not raw:
        print("[entrypoint] CLOWDER_MCP_SERVER_CONFIGS not set, skipping URL resolution")
        return {}

    mapping = json.loads(raw)
    print(f"[entrypoint] Loaded Clowder MCP config: {len(mapping)} server(s)")
    return mapping


def resolve_mcp_urls(run_config, stack_config, clowder):
    """Resolve MCP server URLs from Clowder endpoints using ConfigMap mapping.

    For each MCP server in stack_config["mcp_servers"], looks up the server
    name in the ConfigMap JSON to find the corresponding Clowder app name and
    path suffix.  If a matching Clowder endpoint exists, the server URL is
    replaced with http://{hostname}:{port}{mcp_server_path}.

    Also updates matching tool_runtime providers in run_config so that
    Llama Stack uses the same resolved URLs.
    """
    if not clowder or not getattr(clowder, "endpoints", None):
        return

    mcp_config = load_clowder_mcp_config()
    if not mcp_config:
        return

    tool_runtime_providers = {
        p["provider_id"]: p
        for p in run_config.get("providers", {}).get("tool_runtime", [])
        if p.get("provider_type") == "remote::model-context-protocol"
    }

    for server in stack_config.get("mcp_servers", []):
        mapping = mcp_config.get(server["name"])
        if not mapping:
            continue

        app_name = mapping["clowder_app"]
        service_name = mapping.get("clowder_service")
        mcp_server_path = mapping.get("mcp_server_path", "/")

        endpoint = None
        for ep in clowder.endpoints:
            if ep.app == app_name:
                if service_name and ep.name != service_name:
                    continue
                endpoint = ep
                break

        if endpoint:
            resolved_url = f"http://{endpoint.hostname}:{endpoint.port}{mcp_server_path}"
            server["url"] = resolved_url
            print(f"[entrypoint] Resolved {server['name']} -> {resolved_url}")

            provider = tool_runtime_providers.get(server.get("provider_id"))
            if provider:
                provider["config"]["url"] = resolved_url
                print(f"[entrypoint] Resolved run.yaml provider {provider['provider_id']} -> {resolved_url}")
        else:
            print(f"[entrypoint] No Clowder endpoint for {server['name']} (app={app_name}), keeping existing URL")


def apply_clowder_config(run_config, stack_config, clowder):
    """Apply Clowder ACG config values to the parsed YAML configs."""
    if clowder is None:
        return run_config, stack_config

    # Database config - switch from sqlite to postgres if DB is available
    if clowder.database:
        db = clowder.database
        ssl_mode = getattr(db, "sslMode", None) or "prefer"

        print(f"[entrypoint] Using Clowder DB: {db.hostname}:{db.port}/{db.name}")

        pg_config = {
            "host": db.hostname,
            "port": db.port,
            "db": db.name,
            "user": db.username,
            "password": db.password,
            "ssl_mode": ssl_mode,
        }

        # Write RDS CA cert to file if provided
        rds_ca = getattr(db, "rdsCa", None)
        if rds_ca:
            ca_path = "/tmp/rds-ca.crt"
            with open(ca_path, "w") as f:
                f.write(rds_ca)
            pg_config["ca_cert_path"] = ca_path

        # Update storage backends from sqlite to postgres (llama-stack run.yaml)
        storage = run_config.get("storage", {})
        backends = storage.get("backends", {})

        llama_pg_config = {
            "host": db.hostname,
            "port": db.port,
            "db": db.name,
            "user": db.username,
            "password": db.password,
            "ssl_mode": ssl_mode,
        }

        backends["kv_default"] = {
            "type": "kv_postgres",
            **llama_pg_config,
        }
        backends["sql_default"] = {
            "type": "sql_postgres",
            **llama_pg_config,
        }

        # Update conversation cache in stack config
        stack_config["conversation_cache"] = {
            "type": "postgres",
            "postgres": pg_config,
        }

        # Update main database config in stack config
        stack_config["database"] = {
            "postgres": pg_config,
        }

    # Resolve MCP server URLs from Clowder endpoints
    resolve_mcp_urls(run_config, stack_config, clowder)

    return run_config, stack_config


def render_configs(clowder):
    """Read template YAMLs, apply Clowder config, write to runtime dir.

    Returns the rendered stack config so callers can read values from it.
    """
    os.makedirs(RUNTIME_DIR, exist_ok=True)

    run_template = os.path.join(TEMPLATE_DIR, RUN_YAML)
    stack_template = os.path.join(TEMPLATE_DIR, STACK_YAML)

    with open(run_template) as f:
        run_config = yaml.safe_load(f)

    with open(stack_template) as f:
        stack_config = yaml.safe_load(f)

    run_config, stack_config = apply_clowder_config(run_config, stack_config, clowder)

    run_out = os.path.join(RUNTIME_DIR, RUN_YAML)
    stack_out = os.path.join(RUNTIME_DIR, STACK_YAML)

    with open(run_out, "w") as f:
        yaml.dump(run_config, f, default_flow_style=False, sort_keys=False)

    with open(stack_out, "w") as f:
        yaml.dump(stack_config, f, default_flow_style=False, sort_keys=False)

    print(f"[entrypoint] Wrote {run_out}")
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

    # --- Start lightspeed-stack ---
    service_config = stack_config.get("service", {})
    backend_host = service_config.get("host", "0.0.0.0")
    backend_port = service_config.get("port", 8080)

    backend_url = f"http://{backend_host}:{backend_port}"
    # TODO: Move env var plumbing to a config/env helper
    os.environ.setdefault("PROXY_BACKEND_URL", backend_url)

    print(f"[entrypoint] Starting lightspeed-stack on {backend_host}:{backend_port}...")
    backend = subprocess.Popen(["python3.12", "src/lightspeed_stack.py"])
    children.append(("lightspeed-stack", backend))

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
