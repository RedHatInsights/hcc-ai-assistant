import asyncio
import base64
import hashlib
import json
import os
import re
from pathlib import Path
from string import Template
from typing import Literal
from uuid import UUID

import yaml
from agent_card import mount_agent_card
from conversations import Conversations
from guarded_turn import ConversationChanged, ConversationNotFound, GuardedTurns
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google_cloud import GoogleCloudProvider


class Turn(BaseModel):
    """Console query payload shared by the streaming and non-streaming routes."""

    model_config = ConfigDict(extra="forbid")
    query: str
    provider: Literal["google-vertex"] | None = None
    model: str | None = None
    conversation_id: UUID | None = None
    media_type: Literal["application/json"] = "application/json"
    generate_topic_summary: Literal[False] = False


class ConversationDetails(BaseModel):
    """Public conversation identifier; list entries do not include stored messages."""

    conversation_id: UUID


class ConversationsListResponse(BaseModel):
    """Conversation list for the requesting console user, including an empty list for a new user."""

    conversations: list[ConversationDetails]


def valid_claim(value: object) -> bool:
    """Accept nonblank strings of at most 256 characters without ASCII control characters."""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= 256
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    )


async def require_console_user(request: Request) -> str:
    """Derive a stable conversation owner key from a trusted gateway's User header.

    Hash the organization and user IDs, not the mutable username. Raise HTTP
    401 for a missing header or 400 for invalid encoding or claims. Decoding
    the header does not authenticate its sender.
    """
    header = request.headers.get("x-rh-identity")
    if not header:
        raise HTTPException(401, "Missing x-rh-identity header")
    if len(header) > 8192:
        raise HTTPException(
            400, "x-rh-identity header exceeds maximum allowed size")
    try:
        decoded = base64.b64decode(header, validate=True).decode("utf-8")
    except ValueError as exc:
        raise HTTPException(
            400, "Invalid base64 encoding in x-rh-identity header") from exc
    try:
        claims = json.loads(decoded)
    except (ValueError, RecursionError) as exc:
        raise HTTPException(
            400, "Invalid JSON in x-rh-identity header") from exc
    identity = claims.get("identity") if isinstance(claims, dict) else None
    if not isinstance(identity, dict) or identity.get("type") != "User":
        raise HTTPException(400, "Invalid identity data")
    user = identity.get("user")
    if not isinstance(user, dict) or not all(valid_claim(user.get(key)) for key in ("user_id", "username")):
        raise HTTPException(400, "Invalid identity data")
    if not valid_claim(identity.get("org_id")):
        raise HTTPException(400, "Invalid identity data")
    owner = json.dumps([identity["org_id"], user["user_id"]],
                       separators=(",", ":"))
    return hashlib.sha256(owner.encode()).hexdigest()


def create_app(model: Model | None = None) -> FastAPI:
    """Build the HTTP agent with configured shields, discovery servers, and conversation storage.

    An injected model serves both classification and answering. When no model
    is supplied, construct the Vertex model from the deployment environment.
    """
    config_path = Path(os.getenv("MCP_CONFIG_PATH", Path(
        __file__).resolve().parents[1] / "lightspeed-stack.yaml"))
    config = yaml.safe_load(config_path.read_text())
    servers = {server["name"]               : server for server in config.get("mcp_servers") or []}
    instructions = config["customization"]["system_prompt"]
    shields = {shield["name"]: shield["config"]
               for shield in config["shields"]}
    redaction = shields["pii-redaction"]
    flags = 0 if redaction["case_sensitive"] else re.IGNORECASE
    rules = [(re.compile(rule["pattern"], flags), rule["replacement"])
             for rule in redaction["rules"]]

    def redact(text: str) -> str:
        """Apply the configured PII substitutions to text in rule order."""
        for pattern, replacement in rules:
            text = pattern.sub(replacement, text)
        return text

    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("Missing customization.system_prompt")
    if model is None:
        provider = GoogleCloudProvider(
            project=os.environ["VERTEX_PROJECT"], location=os.environ["VERTEX_LOCATION"])
        model = GoogleModel(os.environ["ALLOWED_MODEL"], provider=provider)
    agent = Agent(model, instructions=instructions)
    classifier = Agent(model, retries=0)
    guard = shields["hcc-topic-guard"]
    prompt = Template(guard["model_prompt"])
    refusal = guard["invalid_question_response"]
    conversations = Conversations()
    turns = GuardedTurns(conversations, agent, classifier, prompt, refusal, redact, servers)
    app = FastAPI()
    mount_agent_card(app)

    @app.get("/readiness")
    async def readiness():
        """Report that the agent initialized; this does not probe Vertex or downstream tools."""
        return {"ready": True}

    @app.get("/v1/conversations", response_model=ConversationsListResponse)
    async def list_conversations(owner: str = Depends(require_console_user)):
        """List the caller's saved conversation IDs without returning message content."""
        ids = await asyncio.to_thread(conversations.list_ids, owner)
        return {"conversations": [{"conversation_id": conversation_id} for conversation_id in ids]}

    @app.post("/v1/query")
    async def query(turn: Turn, request: Request, owner: str = Depends(require_console_user)):
        """Return a completed turn as JSON, including its conversation ID, answer, and token usage."""
        prepared = await prepare_console_turn(turn, request, owner)
        try:
            answer = await prepared.complete()
        except ConversationChanged as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "conversation_id": prepared.conversation_id,
            "response": answer,
            "input_tokens": prepared.usage.input_tokens,
            "output_tokens": prepared.usage.output_tokens,
            "truncated": False,
            "rag_chunks": [],
            "referenced_documents": [],
            "tool_calls": prepared.tool_calls,
            "tool_results": [],
            "available_quotas": {},
        }

    @app.post("/v1/streaming_query")
    async def streaming_query(turn: Turn, request: Request, owner: str = Depends(require_console_user)):
        """Stream a console turn as JSON data records: start, incremental answer text, then end."""
        prepared = await prepare_console_turn(turn, request, owner)

        def frame(event, data, **metadata):
            """Encode one JSON event as a data line followed by a blank line for the stream reader."""
            return "data: " + json.dumps({"event": event, "data": data, **metadata}) + "\n\n"

        async def events():
            """Yield the conversation ID and text deltas, then emit usage only after history is saved."""
            yield frame("start", {"conversation_id": prepared.conversation_id})
            index = 0
            try:
                async for delta in prepared.stream():
                    yield frame("token", {"id": index, "token": delta})
                    index += 1
            except ConversationChanged as exc:
                raise HTTPException(409, str(exc)) from exc
            yield frame("end", {"referenced_documents": [], "truncated": False, "input_tokens": prepared.usage.input_tokens,
                                "output_tokens": prepared.usage.output_tokens, "tool_calls": prepared.tool_calls},
                        available_quotas={})

        return StreamingResponse(events(), media_type="application/json; charset=utf-8")

    async def prepare_console_turn(turn: Turn, request: Request, owner: str):
        try:
            return await turns.prepare(turn.query, turn.conversation_id, owner, request.headers)
        except ConversationNotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    return app
