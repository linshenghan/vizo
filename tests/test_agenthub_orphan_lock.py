import fcntl
import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from agent_hub import AgentHub
from agent_runner import AgentRunner
from lib.confirm_server import ConfirmServer
from lib.settings_handler import ModelConfigManager, make_main_session_role_model_id
from lib.settings_handler import resolve_subagent_codex_candidate
from lib.task_titles import summarize_task_title
from lib.runtime.policy import resolve_subagent_route
from lib.runtime.contracts import RUNTIME_FAMILY_CODEX
from lib.vizo_router_mcp_stdio import VizoRouterAdapter
from lib.web_console import WebConsoleHandler


LONG_DEMO_REQUEST = (
    "统一风格提示词：移动端微信小程序界面，早教 AI 应用，面向 0-6 岁孩子家长。"
    "需要生成 13 个页面/状态的早教 AI 小程序高保真 UI demo。"
)


def test_task_title_summarizes_long_demo_request():
    title = summarize_task_title(LONG_DEMO_REQUEST)

    assert title == "早教AI微信小程序高保真Demo"
    assert len(title) <= 20


def test_agenthub_create_task_holds_lock(tmp_path):
    hub = AgentHub({"task_resource_guard": {"enabled": False}})

    task = hub._create_task(
        "interaction_design",
        "design_demo",
        LONG_DEMO_REQUEST,
        {"path": str(tmp_path), "type": "dev"},
    )

    lock_path = Path(task["task_dir"]) / ".lock"
    assert lock_path.exists()
    assert task["runner_pid"] > 0
    assert task["task_name"] == "早教AI微信小程序高保真Demo"
    assert task["description"] == LONG_DEMO_REQUEST

    with open(lock_path, "r") as lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False

    assert acquired is False

    hub._release_task_lock()
    with open(lock_path, "r") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)


def test_interaction_design_roles_default_to_main_session_model():
    hub = AgentHub({"task_resource_guard": {"enabled": False}})
    manifest, _ = hub._load_and_validate_manifest("interaction_design")

    role_config = hub._resolve_role_config(manifest, "interaction_design", "design_analyst")

    assert role_config["model"] == "__main_session__"
    assert role_config["reasoning_effort"] == "inherit"


def test_model_config_exposes_interaction_design_reasoning_controls():
    models = ModelConfigManager().get_models({"external_models": {"anthropic": {}}})
    roles = {item["role"]: item for item in models["roles"]}

    assert roles["interaction_design.design_analyst"]["current_model"] == "__main_session__"
    assert roles["interaction_design.design_analyst"]["current_reasoning_effort"] == "inherit"
    assert any(item["id"] == "xhigh" for item in models["available_reasoning_efforts"])


def test_openai_role_model_options_use_main_gpt_models_without_legacy_tiers():
    models = ModelConfigManager().get_models(
        {
            "external_models": {
                "anthropic": {
                    "provider_id": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "env": {
                        "ANTHROPIC_DEFAULT_OPUS_MODEL": "gpt-5.4",
                        "ANTHROPIC_DEFAULT_SONNET_MODEL": "gpt-5.4",
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "gpt-5.4",
                    },
                }
            }
        }
    )
    display_by_id = {item["id"]: item["display"] for item in models["available_models"]}
    option_ids = set(display_by_id)

    assert display_by_id["__main_session__"] == "继承主会话模型"
    assert make_main_session_role_model_id("gpt-5.5") in option_ids
    assert make_main_session_role_model_id("gpt-5.4") in option_ids
    assert {"opus", "sonnet", "haiku"}.isdisjoint(option_ids)


def test_claude_role_model_options_use_main_claude_models_without_legacy_tiers():
    models = ModelConfigManager().get_models(
        {
            "external_models": {
                "anthropic": {
                    "provider_id": "anthropic",
                    "base_url": "https://api.anthropic.com",
                    "env": {},
                }
            }
        }
    )
    option_ids = {item["id"] for item in models["available_models"]}

    assert make_main_session_role_model_id("claude-opus-4-7") in option_ids
    assert make_main_session_role_model_id("claude-sonnet-4-7") in option_ids
    assert make_main_session_role_model_id("claude-haiku-4-7") in option_ids
    assert {"opus", "sonnet", "haiku"}.isdisjoint(option_ids)


def test_agent_runner_inherits_main_session_profile(tmp_path, monkeypatch):
    session_dir = tmp_path / ".vizo" / "sessions" / "main" / "ms_test"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "session_id": "ms_test",
                "display_model": "gpt-5.5",
                "provider_model": "gpt-5.5",
                "runtime_family": "codex",
                "metadata": {"reasoning_effort": "high"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("VIZO_MAIN_SESSION_DIR", str(session_dir))

    profile = AgentRunner._load_main_session_profile()
    runner = AgentRunner({"external_models": {"anthropic": {}}})

    assert profile["provider_model"] == "gpt-5.5"
    assert runner._resolve_role_reasoning_effort(
        "design_analyst",
        override="inherit",
        main_session_profile=profile,
    ) == "high"


def test_interaction_design_openai_main_session_routes_to_codex_account_login(tmp_path, monkeypatch):
    session_dir = tmp_path / ".vizo" / "sessions" / "main" / "ms_test"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "session_id": "ms_test",
                "display_model": "gpt-5.5",
                "provider_model": "gpt-5.5",
                "runtime_family": "codex",
                "metadata": {"reasoning_effort": "high"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("VIZO_MAIN_SESSION_DIR", str(session_dir))
    monkeypatch.setattr("lib.runtime.cli_discovery.cli_available", lambda name: name == "codex")
    config = {
        "external_models": {
            "anthropic": {
                "provider_id": "openai",
                "base_url": "https://api.openai.com/v1",
                "auth_mode": "account_login",
                "auth_status": "ready",
                "codex_home": ".vizo/codex/connections/test",
                "env": {},
            }
        }
    }

    candidate = resolve_subagent_codex_candidate(
        config,
        role="design_analyst",
        model_override="__main_session__",
    )
    decision = resolve_subagent_route(
        config=config,
        role="design_analyst",
        model_override="__main_session__",
    )

    assert candidate["selected_model"] == "gpt-5.5"
    assert candidate["profile"]["use_default_auth"] is True
    assert candidate["profile"]["codex_home"] == ".vizo/codex/connections/test"
    assert decision.runtime_family == RUNTIME_FAMILY_CODEX
    assert decision.reason_code == "codex_direct"


def test_lockless_recent_hub_task_is_not_immediately_orphaned(tmp_path):
    server = object.__new__(ConfirmServer)
    task_dir = tmp_path / "hub-20260503-150547-5677"
    task_dir.mkdir()
    state = {
        "id": task_dir.name,
        "module_id": "interaction_design",
        "workflow_id": "design_demo",
        "status": "running",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }

    assert server._should_defer_lockless_hub_orphan_check(task_dir, state) is True


def test_old_lockless_hub_task_can_still_be_marked_orphan(tmp_path):
    server = object.__new__(ConfirmServer)
    task_dir = tmp_path / "hub-20260503-150547-5677"
    task_dir.mkdir()
    state = {
        "id": task_dir.name,
        "module_id": "interaction_design",
        "workflow_id": "design_demo",
        "status": "running",
        "created_at": (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds"),
    }

    assert server._should_defer_lockless_hub_orphan_check(task_dir, state) is False


def test_hub_task_data_uses_short_display_title_for_legacy_state(tmp_path):
    task_dir = tmp_path / "hub-20260503-150547-5677"
    task_dir.mkdir()
    state = {
        "id": task_dir.name,
        "module_id": "interaction_design",
        "workflow_id": "design_demo",
        "status": "failed",
        "description": LONG_DEMO_REQUEST,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "completed_steps": [],
    }

    task_data = WebConsoleHandler._build_hub_task_data(state, task_dir)

    assert task_data["task_name"] == "早教AI微信小程序高保真Demo"
    assert task_data["task_title"] == "早教AI微信小程序高保真Demo"
    assert len(task_data["task_name"]) <= 20
    assert task_data["description"] == LONG_DEMO_REQUEST


def test_background_task_status_marks_failed_launch_inactive(tmp_path):
    task_dir = tmp_path / ".vizo" / "tasks" / "hub-20260503-150547-5677"
    task_dir.mkdir(parents=True)
    (task_dir / "state.json").write_text(
        json.dumps(
            {
                "id": task_dir.name,
                "module_id": "interaction_design",
                "workflow_id": "design_demo",
                "status": "failed",
                "description": LONG_DEMO_REQUEST,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    adapter = VizoRouterAdapter()
    launch = {
        "target_kind": "agent_hub",
        "target_label": "交互设计师",
        "module_id": "interaction_design",
        "request": LONG_DEMO_REQUEST,
        "project_root": str(tmp_path),
        "cwd": str(tmp_path),
        "launched_at": datetime.now().astimezone().isoformat(),
        "pid": 99999999,
    }

    payload = adapter._background_task_status_payload(
        {"last_launch": launch},
        context=SimpleNamespace(project_root=str(tmp_path)),
    )
    message = adapter._format_background_task_status_message(payload)

    assert payload["has_pending_task"] is False
    assert payload["last_launch"]["is_active"] is False
    assert payload["last_launch"]["runtime_status"] == "stale"
    assert payload["last_launch"]["task_status"] == "failed"
    assert payload["last_launch"]["task_id"] == task_dir.name
    assert "已经不在运行" in message
    assert "创建新的后台任务提案" in message


def test_health_reports_disconnected_redis_without_blocking():
    async def run_check():
        server = object.__new__(ConfirmServer)
        server.redis_host = "127.0.0.1"
        server.redis_port = 1
        server._tunnel_url = ""

        started = time.monotonic()
        response = await server.handle_health(None)
        elapsed = time.monotonic() - started
        payload = json.loads(response.text)

        assert response.status == 200
        assert payload["status"] == "ok"
        assert payload["redis"] == "disconnected"
        assert elapsed < 2

    asyncio.run(run_check())
