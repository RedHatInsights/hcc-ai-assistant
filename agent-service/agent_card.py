"""Opt-in A2A 1.0 discovery for the HCC assistant."""

import os

from a2a.server.routes import create_agent_card_routes
from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from fastapi import FastAPI
from pydantic import HttpUrl


def mount_agent_card(app: FastAPI) -> None:
    public_url = os.getenv("A2A_PUBLIC_URL")
    if not public_url:
        return

    url = HttpUrl(public_url)
    if url.scheme != "https" or any(value is not None for value in (url.username, url.password, url.query, url.fragment)):
        raise ValueError("A2A_PUBLIC_URL must be an HTTPS endpoint without credentials, query or fragment")

    card = AgentCard(
        name="Hybrid Cloud Console Assistant",
        description="Answers questions about using and managing the Red Hat Hybrid Cloud Console.",
        version="0.1.0",
        supported_interfaces=[AgentInterface(url=str(url), protocol_binding="JSONRPC", protocol_version="1.0")],
        capabilities=AgentCapabilities(),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[AgentSkill(
            id="console-assistance",
            name="Hybrid Cloud Console assistance",
            description="Help with console workflows, including notifications and access management.",
            tags=["hybrid-cloud-console", "notifications", "access-management"],
            examples=["Where can I manage my console notification settings?", "How do I view my console access?"],
        )],
    )
    card.security_schemes["bearer"].http_auth_security_scheme.scheme = "bearer"
    card.security_requirements.add().schemes["bearer"].SetInParent()
    app.router.routes.extend(create_agent_card_routes(card))