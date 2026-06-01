import importlib.util
import sys
from pathlib import Path


def load_sync_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "sync_public_repo.py"
    spec = importlib.util.spec_from_file_location("sync_public_repo", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_sync_respects_manifest_and_removes_stale_files(tmp_path):
    module = load_sync_module()
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (source / ".env").write_text("TOKEN=do-not-copy\n", encoding="utf-8")
    (source / "notes").mkdir()
    (source / "notes" / "memory.md").write_text("private\n", encoding="utf-8")
    (source / "PUBLIC_SYNC_MANIFEST.txt").write_text(
        "*.py\n!.env\n!notes/**\n",
        encoding="utf-8",
    )
    (target / "old.txt").write_text("remove me\n", encoding="utf-8")
    (target / ".git").mkdir()
    (target / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    report = module.sync_public_repo(
        source=source,
        target=target,
        manifest_path=source / "PUBLIC_SYNC_MANIFEST.txt",
    )

    assert (target / "app.py").exists()
    assert not (target / ".env").exists()
    assert not (target / "notes" / "memory.md").exists()
    assert not (target / "old.txt").exists()
    assert (target / ".git" / "HEAD").exists()
    assert report.copied == ["app.py"]
    assert report.removed == ["old.txt"]


def test_sync_blocks_likely_live_secrets(tmp_path):
    module = load_sync_module()
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    fake_secret = "sk-ant-" + "abcdefghijklmnopqrstuvwxyz"
    (source / "config.py").write_text(
        f"TOKEN = '{fake_secret}'\n",
        encoding="utf-8",
    )
    (source / "PUBLIC_SYNC_MANIFEST.txt").write_text("*.py\n", encoding="utf-8")

    try:
        module.sync_public_repo(
            source=source,
            target=target,
            manifest_path=source / "PUBLIC_SYNC_MANIFEST.txt",
        )
    except module.SyncError as exc:
        assert "Possible live secret" in str(exc)
    else:
        raise AssertionError("expected SyncError")


def test_public_manifest_uses_explicit_root_entrypoints():
    repo_root = Path(__file__).resolve().parents[1]
    manifest_lines = [
        line.strip()
        for line in (repo_root / "PUBLIC_SYNC_MANIFEST.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    assert "*.py" not in manifest_lines
    assert ".env.example" in manifest_lines
    assert "config.json.example" in manifest_lines
    assert "lib/runtime/sessions/*.py" in manifest_lines
    assert "lib/runtime/subagents/*.py" in manifest_lines

    expected_root_entrypoints = {
        "agent_hub.py",
        "agent_router.py",
        "agent_runner.py",
        "opus.py",
        "orchestrator.py",
        "secretary.py",
        "state_manager.py",
        "stream_renderer.py",
        "user_interface.py",
        "vizo.py",
        "wecom_callback.py",
    }
    assert expected_root_entrypoints.issubset(set(manifest_lines))

    removed_root_files = {
        "build.sh",
        "Dockerfile.release",
        "Dockerfile.vizo",
        "docker-compose.vizo.yml",
        "git_helper.sh",
        "RESTART_WEB_CONSOLE.sh",
        "demo_budget_alert.py",
        "demo_improvements.py",
        "fix_subtask_failure.py",
        "orchestrator_fix_subtask_failure.py",
        "test_improvements.py",
        "test_secretary.py",
        "convert_md_to_html.py",
    }
    assert removed_root_files.isdisjoint(set(manifest_lines))
