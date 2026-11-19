import base64
import json
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google_cloud import GoogleCloudProvider


class FirstTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    provider: Literal["google-vertex"] | None = None
    model: str | None = None
    conversation_id: None = None


def valid_claim(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= 256
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    )


async def require_console_user(request: Request) -> None:
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
    if identity.get("org_id") not in (None, "") and not valid_claim(identity["org_id"]):
        raise HTTPException(400, "Invalid identity data")


def create_app(model: Model | None = None) -> FastAPI:
    config_path = Path(os.getenv("MCP_CONFIG_PATH", Path(
        __file__).resolve().parents[1] / "lightspeed-stack.yaml"))
    instructions = yaml.safe_load(config_path.read_text())[
        "customization"]["system_prompt"]
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("Missing customization.system_prompt")
    if model is None:
        provider = GoogleCloudProvider(
            project=os.environ["VERTEX_PROJECT"], location=os.environ["VERTEX_LOCATION"])
        model = GoogleModel(os.environ["ALLOWED_MODEL"], provider=provider)
    agent = Agent(model, instructions=instructions)
    app = FastAPI()

    @app.get("/readiness")
    async def readiness():
        return {"ready": True}

    @app.post("/v1/query", dependencies=[Depends(require_console_user)])
    async def query(turn: FirstTurn):
        result = await agent.run(turn.query)
        usage = result.usage
        return {
            "conversation_id": str(uuid4()),
            "response": result.output,
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
