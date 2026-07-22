import json
import os

import pytest
import yaml

from entrypoint import (
    add_normalized_model_names,
    load_mcp_server_configs,
    merge_mcp_servers,
)


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

    def test_authorization_preserved_after_clowder_config(self, monkeypatch, base_run_config):
        """Authorization access_rules must survive Clowder config application."""
        from entrypoint import apply_clowder_config

        stack_config = {
            "conversation_cache": {"type": "sqlite", "sqlite": {"db_path": "/tmp/test.db"}},
            "authorization": {
                "access_rules": [
                    {"role": "*", "actions": ["query", "list_conversations"]}
                ]
            },
        }

        class FakeDB:
            hostname = "db-host"
            port = 5432
            name = "testdb"
            username = "user"
            password = "pass"
            sslMode = "prefer"
            rdsCa = None

        class FakeClowder:
            database = FakeDB()

        apply_clowder_config(base_run_config, stack_config, FakeClowder())

        # Authorization section must be preserved after Clowder config application
        assert "authorization" in stack_config
        assert len(stack_config["authorization"]["access_rules"]) == 1
        assert stack_config["authorization"]["access_rules"][0]["role"] == "*"

    def test_render_configs_preserves_yaml_access_rules(self, monkeypatch, tmp_path):
        """render_configs must carry the YAML-defined access_rules through."""
        from entrypoint import render_configs

        monkeypatch.setattr("entrypoint.TEMPLATE_DIR", str(tmp_path))
        monkeypatch.setattr("entrypoint.RUNTIME_DIR", str(tmp_path / "out"))
        monkeypatch.delenv("CLOWDER_MCP_SERVER_CONFIGS", raising=False)

        # Minimal run.yaml template
        (tmp_path / "run.yaml").write_text(yaml.dump({"providers": {}}))

        # Stack template with access_rules already defined (as in the real YAML)
        stack = {
            "authorization": {
                "access_rules": [{"role": "*", "actions": ["query", "list_conversations"]}]
            },
        }
        (tmp_path / "lightspeed-stack.yaml").write_text(yaml.dump(stack))

        rendered = render_configs(clowder=None)

        assert "authorization" in rendered
        rules = rendered["authorization"]["access_rules"]
        assert len(rules) == 1
        assert rules[0]["role"] == "*"
        assert "query" in rules[0]["actions"]

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


# ============================================================================
# add_normalized_model_names TESTS
# ============================================================================

class TestAddNormalizedModelNames:

    def test_adds_normalized_name(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_MODEL", "publishers/google/models/gemini-2.5-flash")
        run_config = {
            "providers": {
                "inference": [
                    {"provider_id": "google-vertex", "config": {"allowed_models": ["publishers/google/models/gemini-2.5-flash"]}}
                ]
            }
        }

        add_normalized_model_names(run_config)

        allowed = run_config["providers"]["inference"][0]["config"]["allowed_models"]
        assert "publishers/google/models/gemini-2.5-flash" in allowed
        assert "google/gemini-2.5-flash" in allowed

    def test_skips_non_publishers_format(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_MODEL", "google/gemini-2.5-flash")
        run_config = {
            "providers": {
                "inference": [
                    {"provider_id": "google-vertex", "config": {"allowed_models": ["google/gemini-2.5-flash"]}}
                ]
            }
        }

        add_normalized_model_names(run_config)

        allowed = run_config["providers"]["inference"][0]["config"]["allowed_models"]
        assert allowed == ["google/gemini-2.5-flash"]

    def test_skips_when_no_env_var(self, monkeypatch):
        monkeypatch.delenv("ALLOWED_MODEL", raising=False)
        run_config = {
            "providers": {
                "inference": [
                    {"provider_id": "google-vertex", "config": {"allowed_models": []}}
                ]
            }
        }

        add_normalized_model_names(run_config)

        assert run_config["providers"]["inference"][0]["config"]["allowed_models"] == []

    def test_no_duplicate_if_already_present(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_MODEL", "publishers/google/models/gemini-2.5-flash")
        run_config = {
            "providers": {
                "inference": [
                    {"provider_id": "google-vertex", "config": {"allowed_models": ["publishers/google/models/gemini-2.5-flash", "google/gemini-2.5-flash"]}}
                ]
            }
        }

        add_normalized_model_names(run_config)

        allowed = run_config["providers"]["inference"][0]["config"]["allowed_models"]
        assert allowed.count("google/gemini-2.5-flash") == 1

    def test_skips_provider_without_allowed_models(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_MODEL", "publishers/google/models/gemini-2.5-flash")
        run_config = {
            "providers": {
                "inference": [
                    {"provider_id": "sentence-transformers", "config": {}}
                ]
            }
        }

        add_normalized_model_names(run_config)

        assert "allowed_models" not in run_config["providers"]["inference"][0]["config"]


# ============================================================================
# AUTHORIZATION CONFIG VALIDATION TESTS (RHCLOUD-48660)
# ============================================================================

# Actions required for the HCC AI Assistant to function.
_REQUIRED_ACTIONS = {
    "query",
    "responses",
    "streaming_query",
    "get_conversation",
    "list_conversations",
    "delete_conversation",
    "update_conversation",
    "feedback",
    "get_models",
    "get_tools",
    "info",
}

# Actions that bypass per-user conversation scoping — must NEVER appear in the
# YAML access_rules.
_DANGEROUS_ACTIONS = {
    "admin",
    "list_other_conversations",
    "read_other_conversations",
    "query_other_conversations",
    "delete_other_conversations",
}


def _load_stack_yaml_access_rules():
    """Load access_rules from the lightspeed-stack.yaml template."""
    yaml_path = os.path.join(os.path.dirname(__file__), "lightspeed-stack.yaml")
    with open(yaml_path) as f:
        config = yaml.safe_load(f)
    return config["authorization"]["access_rules"]


class TestYamlAuthorizationRules:
    """Validate that lightspeed-stack.yaml defines correct access_rules,
    excluding dangerous cross-user actions (RHCLOUD-48660).
    """

    def test_has_wildcard_role(self):
        """YAML should define access_rules with role '*'."""
        rules = _load_stack_yaml_access_rules()
        assert len(rules) == 1
        assert rules[0]["role"] == "*"
        assert len(rules[0]["actions"]) > 0

    def test_excludes_dangerous_actions(self):
        """No dangerous action must appear in the YAML access_rules."""
        rules = _load_stack_yaml_access_rules()
        granted = set(rules[0]["actions"])
        dangerous = granted & _DANGEROUS_ACTIONS
        assert not dangerous, (
            f"YAML access_rules must not include dangerous actions: {dangerous}"
        )

    def test_includes_required_actions(self):
        """All actions required for the chatbot must be present in YAML."""
        rules = _load_stack_yaml_access_rules()
        granted = set(rules[0]["actions"])
        missing = _REQUIRED_ACTIONS - granted
        assert not missing, f"YAML access_rules missing required actions: {missing}"

    def test_actions_are_sorted(self):
        """Actions in YAML should be alphabetically sorted for readability."""
        rules = _load_stack_yaml_access_rules()
        actions = rules[0]["actions"]
        assert actions == sorted(actions)
