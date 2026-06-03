import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import vizo_core.agent_hub as agent_hub
from vizo_core.agent_hub import AgentHub, PauseSignal


class _FakeRuntimeController:
    def __init__(self, *args, **kwargs):
        pass

    async def run(self, *, task, step_name, output_file, **kwargs):
        state_file = Path(task["task_dir"]) / "state.json"
        before = json.loads(state_file.read_text(encoding="utf-8"))
        before_mtime = state_file.stat().st_mtime_ns

        assert before["current_step"] == step_name
        assert before["current_role"] == kwargs["role"]
        assert before["current_step_status"] == "running"
        assert before["heartbeat_at"]

        await asyncio.sleep(0.04)
        after_mtime = state_file.stat().st_mtime_ns
        assert after_mtime > before_mtime

        Path(output_file).write_text("done", encoding="utf-8")
        return SimpleNamespace(model="fake-model", runtime_metadata={})


def test_agenthub_state_exposes_current_step_and_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_hub, "SubAgentRuntimeController", _FakeRuntimeController)
    monkeypatch.setattr(agent_hub, "AGENTHUB_STATE_HEARTBEAT_INTERVAL", 0.01)

    hub = AgentHub({"task_resource_guard": {"enabled": False}})
    monkeypatch.setattr(hub, "_inject_project_knowledge", lambda *args, **kwargs: "")

    module_dir = tmp_path / "module"
    (module_dir / "roles").mkdir(parents=True)
    (module_dir / "roles" / "prototype.md").write_text("write the output", encoding="utf-8")

    task = hub._create_task(
        "interaction_design",
        "design_demo",
        "make a prototype",
        {"path": str(tmp_path), "type": "dev"},
    )
    steps = [
        {
            "step": "interaction_demo",
            "role": "prototype_designer",
            "description": "生成交互原型",
            "output": "demo.html",
        }
    ]
    manifest = {
        "roles": {
            "prototype_designer": {
                "template": "roles/prototype.md",
                "model": "sonnet",
                "tools": ["Read"],
            }
        }
    }

    try:
        asyncio.run(hub._execute_steps(task, steps, manifest, module_dir))
    finally:
        hub._release_task_lock()

    state = json.loads((Path(task["task_dir"]) / "state.json").read_text(encoding="utf-8"))
    assert state["current_step"] == "interaction_demo"
    assert state["current_role"] == "prototype_designer"
    assert state["current_step_status"] == "completed"
    assert state["completed_steps"] == ["interaction_demo"]
    assert state["heartbeat_at"]
    assert state["updated_at"]


def test_agenthub_resume_sets_next_pending_step_without_losing_completed_steps(tmp_path, monkeypatch):
    config = {
        "task_resource_guard": {"enabled": False},
        "projects": {"test": {"path": str(tmp_path)}},
    }
    hub = AgentHub(config)
    task = hub._create_task(
        "interaction_design",
        "design_demo",
        "resume prototype task",
        {"path": str(tmp_path), "type": "dev"},
    )
    task["completed_steps"] = ["style_candidates"]
    task["current_step"] = None
    task["current_role"] = None
    hub._save_task_state(task)
    hub._release_task_lock()

    steps = [
        {"step": "style_candidates", "role": "interaction_designer"},
        {"step": "selected_system", "role": "design_system_curator"},
        {"step": "interaction_demo", "role": "prototype_designer"},
    ]

    monkeypatch.setattr(
        hub,
        "_load_and_validate_manifest",
        lambda module_id: ({"workflows": {"design_demo": {}}}, tmp_path),
    )
    monkeypatch.setattr(hub, "_resolve_workflow", lambda manifest, workflow_id: (workflow_id, steps))

    async def _pause_after_resume(resumed_task, *args, **kwargs):
        assert resumed_task["current_step"] == "selected_system"
        assert resumed_task["current_role"] == "design_system_curator"
        assert resumed_task["completed_steps"] == ["style_candidates"]
        raise PauseSignal()

    monkeypatch.setattr(hub, "_execute_steps", _pause_after_resume)

    asyncio.run(hub.execute(resume=True, task_id=task["id"]))
    state = json.loads((Path(task["task_dir"]) / "state.json").read_text(encoding="utf-8"))
    assert state["current_step"] == "selected_system"
    assert state["current_role"] == "design_system_curator"
    assert state["completed_steps"] == ["style_candidates"]
