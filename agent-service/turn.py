import base64
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from string import Template
from typing import Literal
from uuid import UUID, uuid4

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent, ModelMessagesTypeAdapter, UnexpectedModelBehavior
from pydantic_ai.messages import TextPart, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google_cloud import GoogleCloudProvider
from pydantic_ai.usage import RunUsage

from conversations import Conversations
from discovered_tools import turn_tools


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    provider: Literal["google-vertex"] | None = None
    model: str | None = None
    conversation_id: UUID | None = None
    media_type: Literal["application/json"] = "application/json"
    generate_topic_summary: Literal[False] = False


def valid_claim(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= 256
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    )


async def require_console_user(request: Request) -> str:
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
    app = FastAPI()

    @app.get("/readiness")
    async def readiness():
        return {"ready": True}

    @app.post("/v1/query")
    async def query(turn: Turn, request: Request, owner: str = Depends(require_console_user)):
        return await answer_turn(turn, request, owner, streaming=False)

    @app.post("/v1/streaming_query")
    async def streaming_query(turn: Turn, request: Request, owner: str = Depends(require_console_user)):
        return await answer_turn(turn, request, owner, streaming=True)

    async def answer_turn(turn: Turn, request: Request, owner: str, streaming: bool):
        conversation_id = str(turn.conversation_id or uuid4())
        revision, history = 0, []
        if turn.conversation_id is not None:
            previous = await asyncio.to_thread(conversations.load, owner, conversation_id)
            if previous is None:
                raise HTTPException(404, "Conversation not found")
            revision, serialized = previous
            history = ModelMessagesTypeAdapter.validate_json(serialized)
            for message in history:
                for part in message.parts:
                    if isinstance(part, (UserPromptPart, TextPart)) and isinstance(part.content, str):
                        part.content = redact(part.content)
        question = redact(turn.query)
        usage = RunUsage()
        try:
            verdict = await classifier.run(prompt.substitute(message=question, allowed="ALLOWED", rejected="REJECTED"),
                                           usage=usage)
            allowed = verdict.output.strip() == "ALLOWED"
        except UnexpectedModelBehavior:
            allowed = False

        async def save(messages):
            saved = await asyncio.to_thread(conversations.save, owner, conversation_id, revision,
                                            ModelMessagesTypeAdapter.dump_json(messages).decode())
            if not saved:
                raise HTTPException(409, "Conversation changed during turn")

        def frame(event, data, **metadata):
            return "data: " + json.dumps({"event": event, "data": data, **metadata}) + "\n\n"

        async def events():
            yield frame("start", {"conversation_id": conversation_id})
            messages = history
            if allowed:
                async with agent.run_stream(question, message_history=history, usage=usage,
                                            toolsets=[turn_tools(servers, request.headers)]) as result:
                    length = index = 0
                    async for text in result.stream_text(debounce_by=None):
                        if len(text) > length:
                            yield frame("token", {"id": index, "token": text[length:]})
                            length = len(text)
                            index += 1
                    messages = result.all_messages()
            else:
                yield frame("token", {"id": 0, "token": refusal})
            await save(messages)
            yield frame("end", {"referenced_documents": [], "truncated": False, "input_tokens": usage.input_tokens,
                                "output_tokens": usage.output_tokens}, available_quotas={})

        if streaming:
            return StreamingResponse(events(), media_type="application/json; charset=utf-8")
        if allowed:
            result = await agent.run(question, message_history=history, usage=usage,
                                     toolsets=[turn_tools(servers, request.headers)])
            answer = result.output
            history = result.all_messages()
        else:
            answer = refusal
        await save(history)
        return {
            "conversation_id": conversation_id,
            "response": answer,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "truncated": False,
            "rag_chunks": [],
            "referenced_documents": [],
            "tool_calls": [],
            "tool_results": [],
            "available_quotas": {},
        }

    return app
