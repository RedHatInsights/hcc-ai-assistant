"""A2A 1.0 text messages backed by the same guarded turns as console queries."""

import asyncio
from uuid import uuid4

from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import RequestHandler, validate_request_params
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.routes.jsonrpc_dispatcher import JsonRpcDispatcher
from a2a.types.a2a_pb2 import ListTasksResponse, Message, Part, Role, SendMessageRequest
from a2a.utils.errors import (
    ContentTypeNotSupportedError, InternalError, InvalidParamsError, PushNotificationNotSupportedError,
    TaskNotFoundError, UnsupportedOperationError,
)
from fastapi import Depends, FastAPI, Request
from guarded_turn import ConversationChanged, GuardedTurns


class GatewayContext(DefaultServerCallContextBuilder):
    def build(self, request: Request) -> ServerCallContext:
        """Carry the owner verified by the route dependency into the SDK call context."""
        context = super().build(request)
        context.state["owner"] = request.state.conversation_owner
        return context


class DirectMessages(RequestHandler):
    def __init__(self, turns: GuardedTurns):
        """Use the same guarded turn service as the console query routes."""
        self.turns = turns

    @validate_request_params
    async def on_message_send(self, params: SendMessageRequest, context: ServerCallContext) -> Message:
        """Run a plain-text user message in the caller's conversation and reply without creating a task.

        The reply carries the external context ID for follow-ups. A topic-guard refusal
        is returned as text with the HCC-specific ``hccOutcome`` set to ``policy_blocked``.
        """
        message = params.message
        if message.task_id:
            raise TaskNotFoundError()
        if message.role != Role.ROLE_USER:
            raise InvalidParamsError(message="Expected a user message")
        if any(part.WhichOneof("content") != "text" or part.media_type not in ("", "text/plain")
               for part in message.parts):
            raise ContentTypeNotSupportedError(message="Only plain text is supported")
        question = "\n".join(part.text for part in message.parts)
        if not question.strip():
            raise InvalidParamsError(message="Expected nonblank text")
        if params.configuration.HasField("task_push_notification_config"):
            raise PushNotificationNotSupportedError()

        owner = context.state["owner"]
        try:
            mapping = await asyncio.to_thread(self.turns.conversations.resolve_a2a_context, owner,
                                              message.context_id or None)
        except ValueError as exc:
            raise InvalidParamsError(message="Invalid contextId") from exc
        except Exception as exc:
            raise InternalError(message="Unable to resolve conversation") from exc
        try:
            prepared = await self.turns.prepare(question, mapping.conversation_id, owner, context.state["headers"])
            answer = await prepared.complete()
        except ConversationChanged as exc:
            raise InternalError(message="Conversation changed during turn") from exc
        except Exception as exc:
            raise InternalError(message="Unable to complete turn") from exc
        reply = Message(message_id=str(uuid4()), context_id=mapping.context_id, role=Role.ROLE_AGENT,
                        parts=[Part(text=answer)])
        if not prepared.allowed:
            reply.metadata.update({"hccOutcome": "policy_blocked"})
        return reply

    async def on_list_tasks(self, params, context):
        """Return an empty list because direct replies do not create tasks."""
        return ListTasksResponse()

    async def on_get_task(self, params, context):
        """Report task-not-found for lookup or cancellation in this message-only handler."""
        raise TaskNotFoundError()

    on_cancel_task = on_get_task

    def on_message_send_stream(self, params, context):
        """Reject streaming sends and task subscriptions; neither is advertised in the card."""
        raise UnsupportedOperationError()

    on_subscribe_to_task = on_message_send_stream

    async def on_get_extended_agent_card(self, params, context):
        """Reject extended-card requests; discovery exposes only the standard Agent Card."""
        raise UnsupportedOperationError()

    async def on_create_task_push_notification_config(self, params, context):
        """Reject all push notification configuration operations with the corresponding A2A error."""
        raise PushNotificationNotSupportedError()

    on_get_task_push_notification_config = on_create_task_push_notification_config
    on_list_task_push_notification_configs = on_create_task_push_notification_config
    on_delete_task_push_notification_config = on_create_task_push_notification_config


def mount_agent_rpc(app: FastAPI, turns: GuardedTurns, require_owner) -> None:
    """Register the native A2A JSON-RPC endpoint with the supplied owner dependency."""
    dispatcher = JsonRpcDispatcher(request_handler=DirectMessages(turns), context_builder=GatewayContext())

    @app.post("/a2a")
    async def invoke(request: Request, owner: str = Depends(require_owner)):
        """Pass the verified owner to the dispatcher without taking identity from the RPC body."""
        request.state.conversation_owner = owner
        return await dispatcher.handle_requests(request)