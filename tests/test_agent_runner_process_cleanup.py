import asyncio
import signal

from agent_runner import AgentRunner


class _FakeProcess:
    pid = 12345

    def __init__(self):
        self.terminated = False
        self.killed = False
        self.waits = 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waits += 1


def test_graceful_kill_terminates_process_group(monkeypatch):
    sent = []

    monkeypatch.setattr("agent_runner.os.getpgid", lambda pid: 67890)
    monkeypatch.setattr("agent_runner.os.killpg", lambda pgid, sig: sent.append((pgid, sig)))

    runner = object.__new__(AgentRunner)
    proc = _FakeProcess()

    asyncio.run(runner._graceful_kill(proc))

    assert sent == [(67890, signal.SIGTERM)]
    assert not proc.terminated
    assert not proc.killed
    assert proc.waits == 1


def test_signal_process_group_targets_child_group(monkeypatch):
    sent = []

    monkeypatch.setattr("agent_runner.os.getpgid", lambda pid: 67890)
    monkeypatch.setattr("agent_runner.os.killpg", lambda pgid, sig: sent.append((pgid, sig)))

    AgentRunner._signal_process_group(_FakeProcess(), signal.SIGSTOP)

    assert sent == [(67890, signal.SIGSTOP)]
