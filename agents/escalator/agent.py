"""The Escalator as a Google ADK agent.

`EscalatorAgent` is a `BaseAgent` subclass so it runs inside ADK's `Runner` —
the same runtime the other agents in this fleet use. Its `_run_async_impl`
delegates every decision to `escalator.service.run_escalation`. That module
is the only place in this agent that calls a model, and it does so only to
format facts already computed from Firestore into prose (CLAUDE.md Rule 2).
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.genai import types

from common.gcp import get_firestore_client, get_genai_client
from escalator.service import run_escalation


def _extract_claim_id(ctx: InvocationContext) -> str:
    if ctx.user_content is None or not ctx.user_content.parts:
        raise ValueError("escalator invoked with no user_content")
    text = ctx.user_content.parts[0].text
    if not text:
        raise ValueError("escalator invoked with empty message text")
    payload = json.loads(text)
    claim_id = payload.get("claim_id")
    if not claim_id:
        raise ValueError("escalator message missing claim_id")
    return claim_id


class EscalatorAgent(BaseAgent):
    """Drafts a DFS complaint on breach. Never auto-files; never asserts a
    valuation — see CLAUDE.md Rule 3."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        claim_id = _extract_claim_id(ctx)
        db = get_firestore_client()
        genai_client = get_genai_client()

        outcome = await asyncio.to_thread(
            run_escalation, db, genai_client, claim_id
        )

        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        text=json.dumps(
                            {
                                "claim_id": outcome.claim_id,
                                "drafted": outcome.drafted,
                            }
                        )
                    )
                ],
            ),
        )


def build_escalator_agent() -> EscalatorAgent:
    return EscalatorAgent(
        name="escalator",
        description=(
            "Drafts a Florida DFS complaint on a clock.breach event. The "
            "only LLM call in this agent — drafting only, never valuation."
        ),
    )
