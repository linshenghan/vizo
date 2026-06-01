import json
import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

agent_runner_stub = types.ModuleType("agent_runner")
agent_runner_stub.AgentRunner = object
agent_runner_stub.AgentResult = object
agent_runner_stub.AgentTimeoutError = Exception
agent_runner_stub.AgentError = Exception
agent_runner_stub.AgentRateLimitError = Exception
agent_runner_stub.AgentSignalInterrupt = Exception
sys.modules.setdefault("agent_runner", agent_runner_stub)

from orchestrator import Orchestrator
from state_manager import Task


def _make_orchestrator(tmp_path):
    orch = object.__new__(Orchestrator)
    orch.config = {}
    orch.state = SimpleNamespace(
        project_path=tmp_path,
        _save_state=lambda task: task.dir.joinpath("state.json").write_text(
            json.dumps(
                {
                    "id": task.id,
                    "description": task.description,
                    "dir": str(task.dir),
                    "pending_confirm": task.pending_confirm,
                    "completed_steps": task.completed_steps,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        ),
    )
    orch._progress_redis = None
    orch._step_action_throttle = {}
    orch._current_parent_task_id = None
    orch._current_sub_id = None
    return orch


def _make_task(tmp_path, task_id="task-progress"):
    task_dir = tmp_path / ".vizo" / "tasks" / task_id
    task_dir.mkdir(parents=True)
    return Task(
        id=task_id,
        description="progress test",
        dir=task_dir,
        status="running",
        current_step="backend_dev",
        created_at=datetime.now(),
    )


def _read_progress(task):
    return json.loads((task.dir / "progress.json").read_text(encoding="utf-8"))


def test_agent_start_archives_stale_error_row_for_same_step(tmp_path):
    orch = _make_orchestrator(tmp_path)
    task = _make_task(tmp_path)
    (task.dir / "progress.json").write_text(
        json.dumps(
            {
                "task_id": task.id,
                "status": "running",
                "steps": [
                    {
                        "name": "backend_dev",
                        "role": "backend_developer",
                        "status": "error",
                        "error": "old CODEX_HOME failure",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    orch._update_progress(
        task,
        "agent_start",
        role="backend_developer",
        model="gpt",
        step_name="backend_dev",
        attempt=2,
    )

    progress = _read_progress(task)
    assert [step["status"] for step in progress["steps"]] == ["running"]
    assert progress["steps"][0]["attempt"] == 2
    assert progress["steps"][0]["attempt_key"] == "backend_dev#attempt-2"
    assert progress["attempt_history"][0]["error"] == "old CODEX_HOME failure"


def test_agent_complete_syncs_completed_steps_and_step_status(tmp_path):
    orch = _make_orchestrator(tmp_path)
    task = _make_task(tmp_path)
    task.completed_steps = ["qa_test_cases"]
    (task.dir / "progress.json").write_text(
        json.dumps(
            {
                "task_id": task.id,
                "status": "running",
                "completed_steps": [],
                "steps": [
                    {
                        "name": "qa_test_cases",
                        "role": "qa_engineer",
                        "status": "running",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    orch._update_progress(
        task,
        "agent_complete",
        role="qa_engineer",
        step_name="qa_test_cases",
        duration=1.2,
    )

    progress = _read_progress(task)
    assert progress["completed_steps"] == ["qa_test_cases"]
    assert progress["steps"][0]["status"] == "completed"


def test_waiting_confirm_persists_fallback_request_id(tmp_path):
    orch = _make_orchestrator(tmp_path)
    task = _make_task(tmp_path)
    pending = {
        "request_id": "",
        "type": "confirm_with_feedback",
        "summary": "review",
    }

    orch._update_progress(task, "waiting_confirm", pending_confirm=pending)

    progress = _read_progress(task)
    assert progress["pending_confirm"]["request_id"] == f"vizo-{task.id}-pending-confirm"
    assert task.pending_confirm["request_id"] == f"vizo-{task.id}-pending-confirm"


def test_step_action_updates_durable_progress_current_action(tmp_path):
    orch = _make_orchestrator(tmp_path)
    task = _make_task(tmp_path)
    (task.dir / "progress.json").write_text(
        json.dumps(
            {
                "task_id": task.id,
                "status": "running",
                "current_step": "integration",
                "steps": [
                    {
                        "name": "integration",
                        "role": "integration_engineer",
                        "status": "running",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    orch._persist_step_action_progress(
        task.id,
        "integration",
        {"type": "exec", "target": "pytest", "snippet": "pytest tests", "timestamp": "12:00:00"},
    )

    progress = _read_progress(task)
    assert progress["current_action"]["target"] == "pytest"
    assert progress["current_phase"] == "running"
    assert progress["last_event_age_seconds"] == 0
    assert progress["steps"][0]["current_action"]["snippet"] == "pytest tests"
