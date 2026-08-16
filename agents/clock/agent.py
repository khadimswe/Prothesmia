"""The Clock as a Google ADK agent.

`ClockAgent` is a `BaseAgent` subclass so it runs inside ADK's `Runner` —
the same runtime the other four agents in this fleet use. Its
`_run_async_impl` never touches a model: all decision-making is delegated to
`clock.service.run_clock_tick`, which is pure and independently unit tested.
This satisfies both requirements at once — ADK is the runtime, and the Clock
never calls an LLM (CLAUDE.md Rule 2).
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.genai import types

from clock.service import DEFAULT_BREACH_TOPIC, run_clock_tick
from common.gcp import get_firestore_client, get_project_id, get_publisher_client


class ClockAgent(BaseAgent):
    """Deterministic, non-LLM agent. Evaluates every open claim on tick."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        db = get_firestore_client()
        publisher = get_publisher_client()
        project_id = get_project_id()

        summary = await asyncio.to_thread(
            run_clock_tick,
            db,
            publisher,
            project_id,
            DEFAULT_BREACH_TOPIC,
            None,
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
                                "claims_read": summary.claims_read,
                                "claims_skipped": summary.claims_skipped,
                                "clock_checks_written": summary.clock_checks_written,
                                "clock_checks_already_present": (
                                    summary.clock_checks_already_present
                                ),
                                "breaches_newly_created": (
                                    summary.breaches_newly_created
                                ),
                            }
                        )
                    )
                ],
            ),
        )


def build_clock_agent() -> ClockAgent:
    return ClockAgent(
        name="clock",
        description=(
            "Evaluates every open claim against Fla. Stat. §627.70131 daily. "
            "Deterministic — never calls a model."
        ),
    )
