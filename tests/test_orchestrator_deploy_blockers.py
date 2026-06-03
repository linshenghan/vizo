import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

agent_runner_stub = types.ModuleType("agent_runner")
agent_runner_stub.AgentRunner = object
agent_runner_stub.AgentResult = object
agent_runner_stub.AgentTimeoutError = Exception
agent_runner_stub.AgentError = Exception
agent_runner_stub.AgentRateLimitError = Exception
agent_runner_stub.AgentSignalInterrupt = Exception
sys.modules.setdefault("agent_runner", agent_runner_stub)

from vizo_core.orchestrator import Orchestrator
from vizo_core.state_manager import Task


class RecordingUI:
    def __init__(self):
        self.messages = []

    def print_warning(self, message):
        self.messages.append(("warning", message))

    def print_error(self, message):
        self.messages.append(("error", message))

    def print_info(self, message):
        self.messages.append(("info", message))

    def print_success(self, message):
        self.messages.append(("success", message))


class RecordingState:
    def __init__(self):
        self.steps = []

    def update_step(self, task, step_name):
        self.steps.append(step_name)
        task.current_step = step_name


def _make_task(tmp_path):
    task_dir = tmp_path / ".vizo" / "tasks" / "deploy-task"
    task_dir.mkdir(parents=True)
    return Task(
        id="deploy-task",
        description="deploy blocker test",
        dir=task_dir,
        status="running",
        current_step="deploy",
        created_at=datetime.now(),
    )


def _make_orchestrator(results):
    orch = object.__new__(Orchestrator)
    orch.state = RecordingState()
    orch.ui = RecordingUI()
    orch.calls = []
    pending = list(results)

    async def fake_run_agent(**kwargs):
        orch.calls.append(kwargs)
        if kwargs.get("role") != "devops_engineer":
            return SimpleNamespace(data={"status": "success"})
        data = pending.pop(0)
        return SimpleNamespace(data=data)

    async def fake_prepare_frontend_input_docs(task, input_docs, ensure_design_gate):
        return dict(input_docs)

    orch._run_agent = fake_run_agent
    orch._prepare_frontend_input_docs = fake_prepare_frontend_input_docs
    return orch


@pytest.mark.parametrize(
    "deploy_data, category",
    [
        (
            {
                "status": "failed",
                "error_area": "git",
                "error_detail": "fatal: Unable to create '/opt/vizo-next/.git/index.lock': Read-only file system",
            },
            "git",
        ),
        (
            {
                "status": "failed",
                "error_area": "backend",
                "service_healthy": False,
                "error_detail": "Codex sandbox cannot connect to host confirm_server health; PID/listening socket not visible",
            },
            "sandbox",
        ),
        (
            {
                "status": "systemd_blocked",
                "error_area": "devops",
                "error_detail": "systemctl --user failed: Failed to connect to bus",
            },
            "systemd",
        ),
        (
            {
                "status": "failed",
                "error_area": "service_observability",
                "service_healthy": False,
                "error_detail": "service observability failed from deploy runtime",
            },
            "service-observability",
        ),
    ],
)
@pytest.mark.asyncio
async def test_deploy_loop_stops_on_infra_blocker_without_code_fix(tmp_path, deploy_data, category):
    orch = _make_orchestrator([deploy_data])
    task = _make_task(tmp_path)

    await orch._deploy_fix_loop(task, max_rounds=3)

    assert [call["role"] for call in orch.calls] == ["devops_engineer"]
    assert orch.state.steps == ["deploy"]
    error_messages = [message for level, message in orch.ui.messages if level == "error"]
    assert error_messages
    assert category in error_messages[-1]
    assert "已停止自动代码修复循环" in error_messages[-1]


@pytest.mark.asyncio
async def test_deploy_loop_still_routes_product_frontend_failure_to_developer(tmp_path):
    orch = _make_orchestrator(
        [
            {
                "status": "failed",
                "error_area": "frontend",
                "error_detail": "frontend static asset path is broken",
            },
            {"status": "success", "service_healthy": True},
        ]
    )
    task = _make_task(tmp_path)

    await orch._deploy_fix_loop(task, max_rounds=2)

    assert [call["role"] for call in orch.calls] == [
        "devops_engineer",
        "frontend_developer",
        "devops_engineer",
    ]
    assert orch.state.steps == ["deploy", "deploy_round_2"]
