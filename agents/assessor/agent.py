"""The Assessor as a Google ADK agent.

`AssessorAgent` is a `BaseAgent` subclass so it runs inside ADK's `Runner` —
the same runtime the other agents in this fleet use. Its `_run_async_impl`
delegates every decision to `assessor.service.run_assessment`, which is the
only place in this agent that calls a model — and only to describe what is
visible in imagery (CLAUDE.md Rule 2); it never asserts a valuation
(Rule 3).
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

import requests
from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.genai import types

from assessor.imagery import NOAAImageryClient
from assessor.service import run_assessment
from common.gcp import (
    get_artifact_bucket_name,
    get_firestore_client,
    get_genai_client,
    get_storage_client,
)


def _extract_claim_id(ctx: InvocationContext) -> str:
    if ctx.user_content is None or not ctx.user_content.parts:
        raise ValueError("assessor invoked with no user_content")
    text = ctx.user_content.parts[0].text
    if not text:
        raise ValueError("assessor invoked with empty message text")
    payload = json.loads(text)
    claim_id = payload.get("claim_id")
    if not claim_id:
        raise ValueError("assessor message missing claim_id")
    return claim_id


class AssessorAgent(BaseAgent):
    """Fetches before/after parcel imagery on a claim.opened event and
    writes a structured, provenance-linked Gemini finding. Describes only
    what is visible — never asserts a damage valuation (CLAUDE.md Rule 3)."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        claim_id = _extract_claim_id(ctx)
        db = get_firestore_client()
        storage_client = get_storage_client()
        genai_client = get_genai_client()
        imagery_client = NOAAImageryClient(session=requests.Session())
        bucket_name = get_artifact_bucket_name()

        outcome = await asyncio.to_thread(
            run_assessment,
            db,
            storage_client,
            imagery_client,
            genai_client,
            claim_id,
            bucket_name,
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
                                "assessed": outcome.assessed,
                                "insufficient_evidence": outcome.insufficient_evidence,
                            }
                        )
                    )
                ],
            ),
        )


def build_assessor_agent() -> AssessorAgent:
    return AssessorAgent(
        name="assessor",
        description=(
            "Fetches pre/post-event NOAA imagery for a claim's parcel on "
            "claim.opened and runs a structured Gemini multimodal "
            "assessment. Describes what is visible only — never a "
            "valuation."
        ),
    )
