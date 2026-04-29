import json

import pytest

from entrypoint import load_mcp_server_configs, merge_mcp_servers


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def mcp_configs():
    return {
        "rbac-mcp-server": {
            "provider_id": "rbac-mcp-provider",
            "url": "http://rbac:8000/mcp/",
            "headers": ["x-rh-identity"],
        },
        "notifications-mcp-server": {
            "provider_id": "notifications-mcp-provider",
            "url": "http://notifications:8000/mcp/",
        },
    }


@pytest.fixture
def base_run_config():
    return {"providers": {"tool_runtime": []}}


@pytest.fixture
def base_stack_config():
    return {"mcp_servers": []}


# ============================================================================
# load_mcp_server_configs TESTS
# ============================================================================

class TestLoadMcpServerConfigs:

    def test_loads_from_env_var(self, monkeypatch, mcp_configs):
        monkeypatch.setenv("CLOWDER_MCP_SERVER_CONFIGS", json.dumps(mcp_configs))
        result = load_mcp_server_configs()
        assert result == mcp_configs

    def test_loads_from_local_file(self, monkeypatch, tmp_path, mcp_configs):
        monkeypatch.delenv("CLOWDER_MCP_SERVER_CONFIGS", raising=False)
        config_file = tmp_path / "local_mcp_server_configs.json"
        config_file.write_text(json.dumps(mcp_configs))
        monkeypatch.setattr("entrypoint.TEMPLATE_DIR", str(tmp_path))

        result = load_mcp_server_configs()
        assert result == mcp_configs

    def test_returns_empty_when_no_config(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLOWDER_MCP_SERVER_CONFIGS", raising=False)
        monkeypatch.setattr("entrypoint.TEMPLATE_DIR", str(tmp_path))

        result = load_mcp_server_configs()
        assert result == {}

    def test_malformed_json_returns_empty(self, monkeypatch):
        monkeypatch.setenv("CLOWDER_MCP_SERVER_CONFIGS", "{invalid json!!}")
        result = load_mcp_server_configs()
        assert result == {}

    def test_env_var_takes_precedence_over_file(self, monkeypatch, tmp_path):
        env_config = {"env-server": {"provider_id": "env", "url": "http://env/mcp/"}}
        file_config = {"file-server": {"provider_id": "file", "url": "http://file/mcp/"}}

        monkeypatch.setenv("CLOWDER_MCP_SERVER_CONFIGS", json.dumps(env_config))
        config_file = tmp_path / "local_mcp_server_configs.json"
        config_file.write_text(json.dumps(file_config))
        monkeypatch.setattr("entrypoint.TEMPLATE_DIR", str(tmp_path))

        result = load_mcp_server_configs()
        assert "env-server" in result
        assert "file-server" not in result


# ============================================================================
# merge_mcp_servers TESTS
# ============================================================================

class TestMergeMcpServers:

    def test_merges_into_empty_configs(self, monkeypatch, mcp_configs, base_run_config, base_stack_config):
        monkeypatch.setenv("CLOWDER_MCP_SERVER_CONFIGS", json.dumps(mcp_configs))

        merge_mcp_servers(base_run_config, base_stack_config, clowder=None)

        assert len(base_stack_config["mcp_servers"]) == 2
        names = [s["name"] for s in base_stack_config["mcp_servers"]]
        assert "rbac-mcp-server" in names
        assert "notifications-mcp-server" in names

        providers = base_run_config["providers"]["tool_runtime"]
        assert len(providers) == 2
        assert all(p["provider_type"] == "remote::model-context-protocol" for p in providers)

    def test_preserves_headers(self, monkeypatch, mcp_configs, base_run_config, base_stack_config):
        monkeypatch.setenv("CLOWDER_MCP_SERVER_CONFIGS", json.dumps(mcp_configs))

        merge_mcp_servers(base_run_config, base_stack_config, clowder=None)

        rbac = next(s for s in base_stack_config["mcp_servers"] if s["name"] == "rbac-mcp-server")
        assert rbac["headers"] == ["x-rh-identity"]

        notif = next(s for s in base_stack_config["mcp_servers"] if s["name"] == "notifications-mcp-server")
        assert "headers" not in notif

    def test_deduplicates_by_name(self, monkeypatch, base_run_config, base_stack_config):
        base_stack_config["mcp_servers"] = [
            {"name": "existing-server", "provider_id": "old-provider", "url": "http://old/mcp/"}
        ]
        base_run_config["providers"]["tool_runtime"] = [
            {"provider_id": "new-provider", "provider_type": "remote::model-context-protocol", "config": {"url": "http://old/mcp/"}}
        ]

        new_configs = {
            "existing-server": {
                "provider_id": "new-provider",
                "url": "http://new/mcp/",
            }
        }
        monkeypatch.setenv("CLOWDER_MCP_SERVER_CONFIGS", json.dumps(new_configs))

        merge_mcp_servers(base_run_config, base_stack_config, clowder=None)

        assert len(base_stack_config["mcp_servers"]) == 1
        assert base_stack_config["mcp_servers"][0]["url"] == "http://new/mcp/"

    def test_no_configs_is_noop(self, monkeypatch, base_run_config, base_stack_config):
        monkeypatch.delenv("CLOWDER_MCP_SERVER_CONFIGS", raising=False)
        monkeypatch.setattr("entrypoint.TEMPLATE_DIR", "/nonexistent")

        original_run = json.dumps(base_run_config)
        original_stack = json.dumps(base_stack_config)

        merge_mcp_servers(base_run_config, base_stack_config, clowder=None)

        assert json.dumps(base_run_config) == original_run
        assert json.dumps(base_stack_config) == original_stack

    def test_creates_missing_keys(self, monkeypatch, mcp_configs):
        monkeypatch.setenv("CLOWDER_MCP_SERVER_CONFIGS", json.dumps(mcp_configs))
        run_config = {}
        stack_config = {}

        merge_mcp_servers(run_config, stack_config, clowder=None)

        assert "mcp_servers" in stack_config
        assert "tool_runtime" in run_config.get("providers", {})

    def test_skips_server_without_url_or_clowder(self, monkeypatch, base_run_config, base_stack_config):
        configs = {
            "bad-server": {
                "provider_id": "bad-provider",
            }
        }
        monkeypatch.setenv("CLOWDER_MCP_SERVER_CONFIGS", json.dumps(configs))

        merge_mcp_servers(base_run_config, base_stack_config, clowder=None)

        assert len(base_stack_config["mcp_servers"]) == 0
        assert len(base_run_config["providers"]["tool_runtime"]) == 0

    def test_clowder_url_resolution(self, monkeypatch, base_run_config, base_stack_config):
        configs = {
            "my-server": {
                "provider_id": "my-provider",
                "clowder_app": "my-app",
                "mcp_server_path": "/mcp/",
            }
        }
        monkeypatch.setenv("CLOWDER_MCP_SERVER_CONFIGS", json.dumps(configs))

        class FakeEndpoint:
            app = "my-app"
            name = "my-service"
            hostname = "resolved-host"
            port = 9999

        class FakeClowder:
            endpoints = [FakeEndpoint()]

        merge_mcp_servers(base_run_config, base_stack_config, clowder=FakeClowder())

        server = base_stack_config["mcp_servers"][0]
        assert server["url"] == "http://resolved-host:9999/mcp/"

        provider = base_run_config["providers"]["tool_runtime"][0]
        assert provider["config"]["url"] == "http://resolved-host:9999/mcp/"

    def test_clowder_no_matching_endpoint(self, monkeypatch, base_run_config, base_stack_config):
        configs = {
            "my-server": {
                "provider_id": "my-provider",
                "url": "http://fallback/mcp/",
                "clowder_app": "nonexistent-app",
            }
        }
        monkeypatch.setenv("CLOWDER_MCP_SERVER_CONFIGS", json.dumps(configs))

        class FakeEndpoint:
            app = "other-app"
            name = "other-service"
            hostname = "other-host"
            port = 1234

        class FakeClowder:
            endpoints = [FakeEndpoint()]

        merge_mcp_servers(base_run_config, base_stack_config, clowder=FakeClowder())

        server = base_stack_config["mcp_servers"][0]
        assert server["url"] == "http://fallback/mcp/"
