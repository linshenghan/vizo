import json

from lib import mcp_runtime
from lib import settings_handler
from lib.settings_handler import McpServiceManager


def _write_json(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_locked_mcp_services_are_first_and_always_enabled(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    mcp_path = tmp_path / ".mcp.json"
    _write_json(
        config_path,
        {
            "mcp_service_state": {
                "serena": {"enabled": False},
                "vizo-router": {"enabled": False},
                "mcp-chrome": {"enabled": False},
            }
        },
    )
    _write_json(
        mcp_path,
        {
            "mcpServers": {
                "playwright": {"command": "npx"},
                "vizo-router": {"command": "python3"},
                "mcp-chrome": {"command": "python3"},
                "serena": {"command": "serena"},
                "zai": {"command": "npx"},
            }
        },
    )
    monkeypatch.setattr(settings_handler, "_CONFIG_FILE", config_path)
    monkeypatch.setattr(mcp_runtime, "_CONFIG_FILE", config_path)
    monkeypatch.setattr(McpServiceManager, "MCP_JSON_PATH", mcp_path)

    overview = McpServiceManager().get_overview()
    assert [item["name"] for item in overview["services"][:2]] == ["serena", "vizo-router"]
    assert "core_services" not in overview
    assert "other_services" not in overview

    services = {item["name"]: item for item in overview["services"]}
    assert services["serena"]["enabled"] is True
    assert services["serena"]["is_locked"] is True
    assert services["serena"]["can_toggle"] is False
    assert services["vizo-router"]["enabled"] is True
    assert services["vizo-router"]["is_locked"] is True
    assert services["vizo-router"]["can_toggle"] is False
    assert services["mcp-chrome"]["enabled"] is False


def test_locked_mcp_services_reject_disable_requests(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    mcp_path = tmp_path / ".mcp.json"
    _write_json(config_path, {"mcp_service_state": {}})
    _write_json(
        mcp_path,
        {"mcpServers": {"serena": {"command": "serena"}, "vizo-router": {"command": "python3"}}},
    )
    monkeypatch.setattr(settings_handler, "_CONFIG_FILE", config_path)
    monkeypatch.setattr(McpServiceManager, "MCP_JSON_PATH", mcp_path)

    manager = McpServiceManager()
    assert manager.update_service_states({"serena": False}) == "Serena 是系统内置 MCP，不能关闭"
    assert manager.update_service_states({"vizo-router": False}) == "Vizo Router 是系统内置 MCP，不能关闭"


def test_locked_mcp_services_ignore_disabled_config():
    config = {
        "mcp_service_state": {
            "serena": {"enabled": False},
            "vizo-router": {"enabled": False},
        }
    }
    assert mcp_runtime.is_mcp_service_enabled("serena", config) is True
    assert mcp_runtime.is_mcp_service_enabled("vizo-router", config) is True
