import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vizo_core.agent_runner import AgentResult
from vizo_core.orchestrator import Orchestrator
from vizo_core.state_manager import Task, WorkflowError


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
        self.started = []
        self.completed = []
        self.saved = 0
        self.rolled_back = False

    def update_step(self, task, step_name):
        self.started.append(step_name)
        task.current_step = step_name

    def complete_step(self, task, step_name):
        self.completed.append(step_name)
        if step_name not in task.completed_steps:
            task.completed_steps.append(step_name)

    def _save_state(self, task):
        self.saved += 1

    def rollback(self, task):
        self.rolled_back = True


def _agent_result(data):
    return AgentResult(
        success=data.get("all_passed", False),
        data=data,
        raw_output="",
        cost_tokens=0,
        duration=0,
        exit_code=0,
    )


def _make_task(tmp_path):
    task_dir = tmp_path / ".vizo" / "tasks" / "qa-loop-task"
    task_dir.mkdir(parents=True)
    return Task(
        id="qa-loop-task",
        description="QA loop blocker test",
        dir=task_dir,
        status="running",
        created_at=datetime.now(),
    )


def _make_orchestrator(qa_results):
    orch = object.__new__(Orchestrator)
    orch.state = RecordingState()
    orch.ui = RecordingUI()
    orch.calls = []
    pending_qa = list(qa_results)

    async def fake_run_auto_regression(task):
        return None

    def fake_run_change_impact_analysis(task):
        return None

    def fake_run_lint_check(task):
        return None

    def fake_get_scope_files(task):
        return []

    async def fake_run_agent(**kwargs):
        orch.calls.append(kwargs)
        if kwargs["role"] == "qa_engineer":
            return _agent_result(pending_qa.pop(0))
        return _agent_result({"status": "fixed", "all_passed": True})

    orch._run_auto_regression = fake_run_auto_regression
    orch._run_change_impact_analysis = fake_run_change_impact_analysis
    orch._run_lint_check = fake_run_lint_check
    orch._get_scope_files = fake_get_scope_files
    orch._run_agent = fake_run_agent
    orch._update_progress = lambda *args, **kwargs: None
    orch._get_current_commit_hash = lambda: "abc123"
    orch._build_fix_impact_doc = lambda start_hash: None
    return orch


@pytest.mark.asyncio
async def test_qa_environment_blocked_without_failures_does_not_start_fix_round(tmp_path):
    orch = _make_orchestrator([
        {
            "all_passed": False,
            "failures": [],
            "summary": (
                "QA 只发现浏览器环境阻塞：Playwright MCP user cancelled MCP tool call，"
                "confirm_server failed to connect to 127.0.0.1"
            ),
            "total": 0,
            "passed": 0,
        }
    ])
    task = _make_task(tmp_path)

    with pytest.raises(WorkflowError, match="QA 环境/验证阻塞"):
        await orch._test_fix_loop(task, max_rounds=3)

    assert [call["role"] for call in orch.calls] == ["qa_engineer"]
    assert "test_blocked" in task.completed_steps
    assert any("未发现可行动产品缺陷" in msg for level, msg in orch.ui.messages if level == "error")


@pytest.mark.asyncio
async def test_disabled_control_fixture_blocker_does_not_start_fix_round(tmp_path):
    orch = _make_orchestrator([
        {
            "all_passed": False,
            "failures": [
                {
                    "test": "draft persistence browser fixture",
                    "area": "frontend",
                    "file": "lib/templates/dialogue_console/pc.html",
                    "detail": (
                        "page.fill: Timeout 30000ms exceeded on "
                        '<textarea disabled id="composerInput">; session fixture not initialized'
                    ),
                }
            ],
            "total": 1,
            "passed": 0,
        }
    ])
    task = _make_task(tmp_path)

    with pytest.raises(WorkflowError, match="QA 环境/验证阻塞"):
        await orch._test_fix_loop(task, max_rounds=3)

    assert [call["role"] for call in orch.calls] == ["qa_engineer"]
    assert "test_blocked" in task.completed_steps


@pytest.mark.parametrize(
    "detail",
    [
        "screenshot failed: ENOENT no such file or directory, open artifacts/dialogue.png",
        "browser evaluate failed: ReferenceError: require is not defined when calling require('fs')",
    ],
)
def test_validator_implementation_failures_are_classified_as_non_product(detail):
    blocker = Orchestrator._classify_qa_non_product_blocker(
        {
            "all_passed": False,
            "failures": [
                {
                    "test": "browser validator",
                    "area": "frontend",
                    "file": "tests/browser-validator.js",
                    "detail": detail,
                }
            ],
        },
        [
            {
                "test": "browser validator",
                "area": "frontend",
                "file": "tests/browser-validator.js",
                "detail": detail,
            }
        ],
    )

    assert blocker
    assert blocker["category"] == "qa-environment"


@pytest.mark.asyncio
async def test_real_product_failure_still_enters_fix_loop(tmp_path):
    orch = _make_orchestrator([
        {
            "all_passed": False,
            "failures": [
                {
                    "test": "theme toggle persists selected theme",
                    "area": "frontend",
                    "file": "lib/templates/dialogue_console/runtime.js",
                    "detail": "theme toggle writes the wrong localStorage key",
                }
            ],
            "total": 1,
            "passed": 0,
        },
        {
            "all_passed": True,
            "failures": [],
            "total": 1,
            "passed": 1,
        },
    ])
    task = _make_task(tmp_path)

    await orch._test_fix_loop(task, max_rounds=3)

    assert [call["role"] for call in orch.calls] == [
        "qa_engineer",
        "frontend_developer",
        "qa_engineer",
    ]
    assert "test_passed" in task.completed_steps
