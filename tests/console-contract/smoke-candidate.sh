#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."

context=$(docker context show)
case "$(docker context inspect "$context" --format '{{.Endpoints.docker.Host}}')" in
  unix://*) ;;
  *) echo 'A local Docker socket is required' >&2; exit 1 ;;
esac

image=$(tar -cf - agent-service/{Dockerfile.candidate,pyproject.toml,uv.lock,turn.py,conversations.py,discovered_tools.py} \
    lightspeed-stack.yaml proxy.py | docker --context "$context" build --builder "$context" -q -f agent-service/Dockerfile.candidate -)
echo "Agent/proxy candidate: $image"
docker --context "$context" run -i --network none --read-only --tmpfs /tmp:mode=1777 --cap-drop ALL \
    --security-opt no-new-privileges -e AGENT_SQLITE_PATH=/tmp/smoke.sqlite \
    -e VERTEX_PROJECT=synthetic -e VERTEX_LOCATION=us-central1 -e ALLOWED_MODEL=gemini-test \
    "$image" python - <<'PY'
import subprocess
import time

import httpx


def start(app, port, *options):
    return subprocess.Popen(["uvicorn", app, "--host", "127.0.0.1", "--port", str(port), "--no-access-log", *options])


def wait_ready(process, port):
    deadline = time.monotonic() + 45
    while process.poll() is None and time.monotonic() < deadline:
        try:
            if client.get(f"http://127.0.0.1:{port}/readiness").status_code == 200:
                return
        except httpx.ConnectError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"port {port} not ready; process status {process.poll()}")


with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False) as client:
    agent = start("turn:create_app", 8080, "--factory", "--app-dir", "/app/agent-service")
    wait_ready(agent, 8080)
    proxy = start("proxy:app", 8000)
    wait_ready(proxy, 8000)
    response = client.post("/api/ai-assistant/v1/query", json={"query": "synthetic"})
    assert response.status_code == 401 and response.headers['content-type'] == 'application/json'
    assert isinstance(response.json()['detail'], str)
    assert client.get('/liveness').status_code == 200
    agent.terminate()
    agent.wait(timeout=10)
    assert client.get('/readiness').status_code == 503
    assert client.get('/liveness').status_code == 200
    proxy.terminate()
    proxy.wait(timeout=10)
    print('PASS: proxy readiness, missing-identity query, and liveness after backend loss')
PY