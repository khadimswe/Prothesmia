"""Cloud Run entrypoint.

Exposes:
  GET  /healthz          liveness check
  POST /tasks/clock-tick Pub/Sub push target for the `clock.tick` topic

The push subscription must be created with an OIDC token bound to a service
account holding `roles/run.invoker` on this service, and the Cloud Run
service must NOT allow unauthenticated invocations. That is enforced at the
subscription/IAM layer, not in this handler.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request, Response
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from clock.agent import build_clock_agent
from common.logging_config import configure_logging

configure_logging()
logger = logging.getLogger("prothesmia.main")

app = FastAPI(title="prothesmia-agents")

_APP_NAME = "prothesmia-agents"
_session_service = InMemorySessionService()
_clock_runner = Runner(
    app_name=_APP_NAME,
    agent=build_clock_agent(),
    session_service=_session_service,
)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/tasks/clock-tick")
async def clock_tick(request: Request) -> Response:
    # The Pub/Sub push envelope's message content is not read — the tick
    # carries no payload the Clock needs. Its arrival is the trigger.
    await request.body()

    session = await _session_service.create_session(
        app_name=_APP_NAME, user_id="scheduler"
    )
    invocation_id = str(uuid.uuid4())
    try:
        async for event in _clock_runner.run_async(
            user_id="scheduler",
            session_id=session.id,
            invocation_id=invocation_id,
            new_message=types.Content(
                role="user", parts=[types.Part(text="clock.tick")]
            ),
        ):
            logger.info(
                "clock_tick.event", extra={"author": event.author}
            )
    except Exception:
        logger.exception("clock_tick.failed")
        # Non-2xx so Pub/Sub retries; the subscription's DLQ policy takes
        # over after max delivery attempts.
        return Response(status_code=500)

    return Response(status_code=204)
