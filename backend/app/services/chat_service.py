"""Conversation orchestration: history -> prompt -> LLM -> atomic persistence."""

from __future__ import annotations

import asyncio
import logging
import weakref

from app.db import Database
from app.llm.client import CompletionResult, FailoverLLMClient
from app.llm.simulation import FailurePlan
from app.schemas import ChatMetadata, ChatResponse, HistoryMessage

logger = logging.getLogger(__name__)


class SessionNotFoundError(Exception):
    pass


class ChatService:
    def __init__(
        self,
        db: Database,
        llm: FailoverLLMClient,
        default_system_prompt: str,
        max_history_messages: int,
    ) -> None:
        self.db = db
        self.llm = llm
        self.default_system_prompt = default_system_prompt
        self.max_history_messages = max_history_messages
        # One lock per active session: serialises turns of the same conversation so two
        # concurrent requests can't both build on the same (stale) history.
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def chat(
        self,
        session_id: str,
        message: str,
        system_prompt: str | None,
        failure_plan: FailurePlan | None = None,
    ) -> ChatResponse:
        async with self._lock_for(session_id):
            session = await self.db.get_session(session_id)
            effective_prompt = system_prompt or (session.system_prompt if session else None) or self.default_system_prompt

            history = await self.db.get_messages(session_id, limit=self.max_history_messages) if session else []
            llm_messages: list[dict[str, str]] = [{"role": "system", "content": effective_prompt}]
            llm_messages += [{"role": m.role, "content": m.content} for m in history]
            llm_messages.append({"role": "user", "content": message})

            # Raises on total failure -> nothing below runs, nothing is persisted.
            result = await self.llm.complete(llm_messages, failure_plan=failure_plan)

            metadata = self._metadata(result)
            await self.db.save_turn(
                session_id=session_id,
                system_prompt=effective_prompt,
                user_content=message,
                assistant_content=result.reply,
                assistant_metadata=metadata.model_dump(),
            )
            return ChatResponse(session_id=session_id, reply=result.reply, metadata=metadata)

    async def history(self, session_id: str) -> list[HistoryMessage]:
        session = await self.db.get_session(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        stored = await self.db.get_messages(session_id)
        return [HistoryMessage(role=m.role, content=m.content, metadata=m.metadata) for m in stored]

    @staticmethod
    def _metadata(result: CompletionResult) -> ChatMetadata:
        return ChatMetadata(
            resolution_source=result.resolution_source,  # type: ignore[arg-type]
            model=result.model,
            latency_ms=result.latency_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            key_id=result.key_id,
            fallback_attempts=result.fallback_attempts,
        )
