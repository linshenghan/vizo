import argparse
import asyncio

from lib import confirm_server
from lib.confirm_server import ConfirmServer


def test_status_reports_port_occupied_when_pid_file_is_stale(monkeypatch, capsys):
    monkeypatch.setattr(confirm_server, "read_pid", lambda: 12345)
    monkeypatch.setattr(confirm_server, "is_running", lambda: False)
    monkeypatch.setattr(
        confirm_server,
        "_load_confirm_server_runtime_config",
        lambda: ("0.0.0.0", 9390),
    )
    monkeypatch.setattr(confirm_server, "_is_port_listening", lambda host, port: True)
    monkeypatch.setattr(
        confirm_server,
        "_probe_confirm_server_health",
        lambda host, port: {"ok": False, "payload": {}, "error": "not vizo"},
    )

    confirm_server.cmd_status(argparse.Namespace(port=None))

    output = capsys.readouterr().out
    assert "确认服务: 端口已占用" in output
    assert "PID" not in output.splitlines()[0]
    assert "监听检查: http://127.0.0.1:9390 已占用" in output
    assert "健康检查: 异常" in output


def test_status_reports_healthy_service_without_valid_pid(monkeypatch, capsys):
    monkeypatch.setattr(confirm_server, "read_pid", lambda: None)
    monkeypatch.setattr(confirm_server, "is_running", lambda: False)
    monkeypatch.setattr(
        confirm_server,
        "_load_confirm_server_runtime_config",
        lambda: ("0.0.0.0", 9390),
    )
    monkeypatch.setattr(confirm_server, "_is_port_listening", lambda host, port: True)
    monkeypatch.setattr(
        confirm_server,
        "_probe_confirm_server_health",
        lambda host, port: {"ok": True, "payload": {"status": "ok"}, "error": ""},
    )

    confirm_server.cmd_status(argparse.Namespace(port=None))

    output = capsys.readouterr().out
    assert "确认服务: 运行中（PID 文件缺失或过期）" in output
    assert "健康检查: 正常" in output


def test_favicon_returns_empty_success_response():
    async def run_check():
        server = object.__new__(ConfirmServer)
        response = await server.handle_favicon(None)

        assert response.status == 204
        assert response.body in (None, b"")

    asyncio.run(run_check())
