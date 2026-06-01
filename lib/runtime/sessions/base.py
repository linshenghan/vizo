from __future__ import annotations

from abc import ABC, abstractmethod


class BaseMainSessionAdapter(ABC):
    runtime_family = ""
    supports_persistent = False

    @abstractmethod
    async def run(
        self,
        *,
        decision,
        session,
        turn_id: str,
        prompt: str,
        connection: dict,
        on_stream_event=None,
        on_raw_output=None,
    ):
        raise NotImplementedError
