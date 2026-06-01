from __future__ import annotations

from .base import BaseSubagentAdapter


class ClaudeSubagentAdapter(BaseSubagentAdapter):
    runtime_family = "claude_code"

    def __init__(self, agent_runner) -> None:
        self.agent_runner = agent_runner

    async def run(self, *, decision, execution_context, **kwargs):
        return await self.agent_runner.run(**kwargs)
