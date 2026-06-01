from __future__ import annotations

from abc import ABC, abstractmethod


class BaseSubagentAdapter(ABC):
    runtime_family = ""

    @abstractmethod
    async def run(self, *, decision, execution_context, **kwargs):
        raise NotImplementedError
