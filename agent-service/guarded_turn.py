"""Owner-scoped agent execution shared by transport adapters."""

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from string import Template
from uuid import UUID, uuid4

from conversations import Conversations
from discovered_tools import turn_tools
from pydantic_ai import Agent, ModelMessagesTypeAdapter, UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, TextPart, UserPromptPart
from pydantic_ai.usage import RunUsage


class ConversationNotFound(Exception):
    pass


class ConversationChanged(Exception):
    pass


@dataclass
class GuardedTurns:
    conversations: Conversations
    agent: Agent
    classifier: Agent
    prompt: Template
    refusal: str
    redact: Callable[[str], str]
    servers: dict

    async def prepare(self, query: str, conversation_id: UUID | None, owner: str,
                      headers: Mapping[str, str]) -> "PreparedTurn":
        identifier = str(conversation_id or uuid4())
        revision, history = 0, []
        if conversation_id is not None:
            previous = await asyncio.to_thread(self.conversations.load, owner, identifier)
            if previous is None:
                raise ConversationNotFound("Conversation not found")
            revision, serialized = previous
            history = ModelMessagesTypeAdapter.validate_json(serialized)
            for message in history:
                for part in message.parts:
                    if isinstance(part, (UserPromptPart, TextPart)) and isinstance(part.content, str):
                        part.content = self.redact(part.content)
        question = self.redact(query)
        usage = RunUsage()
        try:
            verdict = await self.classifier.run(
                self.prompt.substitute(message=question, allowed="ALLOWED", rejected="REJECTED"), usage=usage)
            allowed = verdict.output.strip() == "ALLOWED"
        except UnexpectedModelBehavior:
            allowed = False
        return PreparedTurn(self, identifier, owner, revision, history, question, headers, usage, allowed)


@dataclass
class PreparedTurn:
    service: GuardedTurns
    conversation_id: str
    owner: str
    revision: int
    history: list[ModelMessage]
    question: str
    headers: Mapping[str, str] = field(repr=False)
    usage: RunUsage
    allowed: bool
    tool_calls: list[dict[str, str]] = field(default_factory=list)

    def _tools(self):
        return [turn_tools(self.service.servers, self.headers, self.tool_calls)]

    async def _save(self, messages: list[ModelMessage]) -> None:
        saved = await asyncio.to_thread(self.service.conversations.save, self.owner, self.conversation_id,
                                        self.revision, ModelMessagesTypeAdapter.dump_json(messages).decode())
        if not saved:
            raise ConversationChanged("Conversation changed during turn")

    async def complete(self) -> str:
        if not self.allowed:
            await self._save(self.history)
            return self.service.refusal
        result = await self.service.agent.run(self.question, message_history=self.history, usage=self.usage,
                                              toolsets=self._tools())
        await self._save(result.all_messages())
        return result.output

    async def stream(self) -> AsyncIterator[str]:
        if not self.allowed:
            yield self.service.refusal
            await self._save(self.history)
            return
        async with self.service.agent.run_stream(self.question, message_history=self.history, usage=self.usage,
                                                 toolsets=self._tools()) as result:
            length = 0
            async for text in result.stream_text(debounce_by=None):
                if len(text) > length:
                    yield text[length:]
                    length = len(text)
            messages = result.all_messages()
        await self._save(messages)