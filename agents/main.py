"""Cloud Run entrypoint.

Exposes:
  GET  /healthz          liveness check
  POST /tasks/clock-tick Pub/Sub push target for the `clock.tick` topic
  POST /tasks/breach     Pub/Sub push target for the `clock.breach` topic
  POST /tasks/assess     Pub/Sub push target for the `claim.opened` topic

The push subscriptions must be created with an OIDC token bound to a service
account holding `roles/run.invoker` on this service, and the Cloud Run
service must NOT allow unauthenticated invocations. That is enforced at the
subscription/IAM layer, not in these handlers.

The `claim.opened` topic and its push subscription to `/tasks/assess` are
referenced here but not provisioned by this repo — see
`assessor.service.CLAIM_OPENED_TOPIC`.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid

from fastapi import FastAPI, Request, Response
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from assessor.agent import build_assessor_agent
from clock.agent import build_clock_agent
from common.logging_config import configure_logging
from escalator.agent import build_escalator_agent

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
_escalator_runner = Runner(
    app_name=_APP_NAME,
    agent=build_escalator_agent(),
    session_service=_session_service,
)
_assessor_runner = Runner(
    app_name=_APP_NAME,
    agent=build_assessor_agent(),
    session_service=_session_service,
)


def _claim_id_from_pubsub_envelope(envelope: dict) -> str:
    """Extract claim_id from a Pub/Sub push envelope.

    The Clock publishes `claim_id` both as a message attribute and inside
    the JSON message body; the attribute is read first since it needs no
    decoding.
    """
    message = envelope.get("message") or {}
    attributes = message.get("attributes") or {}
    claim_id = attributes.get("claim_id")
    if claim_id:
        return claim_id
    data_b64 = message.get("data")
    if not data_b64:
        raise ValueError("pubsub push envelope missing message.data")
    payload = json.loads(base64.b64decode(data_b64))
    claim_id = payload.get("claim_id")
    if not claim_id:
        raise ValueError("pubsub message missing claim_id")
    return claim_id


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


@app.post("/tasks/breach")
async def clock_breach(request: Request) -> Response:
    body = await request.body()
    try:
        claim_id = _claim_id_from_pubsub_envelope(json.loads(body))
    except (ValueError, json.JSONDecodeError):
        logger.exception("clock_breach.bad_envelope")
        # A malformed envelope will never parse on redelivery either; ack
        # it so Pub/Sub does not retry a message we can never read.
        return Response(status_code=204)

    session = await _session_service.create_session(
        app_name=_APP_NAME, user_id="escalator"
    )
    invocation_id = str(uuid.uuid4())
    try:
        async for event in _escalator_runner.run_async(
            user_id="escalator",
            session_id=session.id,
            invocation_id=invocation_id,
            new_message=types.Content(
                role="user",
                parts=[types.Part(text=json.dumps({"claim_id": claim_id}))],
            ),
        ):
            logger.info(
                "clock_breach.event",
                extra={"claim_id": claim_id, "author": event.author},
            )
    except Exception:
        logger.exception("clock_breach.failed", extra={"claim_id": claim_id})
        # Non-2xx so Pub/Sub retries; run_escalation's create-if-absent
        # guard on `escalations` makes a retry safe.
        return Response(status_code=500)

    return Response(status_code=204)


@app.post("/tasks/assess")
async def claim_opened(request: Request) -> Response:
    body = await request.body()
    try:
        claim_id = _claim_id_from_pubsub_envelope(json.loads(body))
    except (ValueError, json.JSONDecodeError):
        logger.exception("claim_opened.bad_envelope")
        # A malformed envelope will never parse on redelivery either; ack
        # it so Pub/Sub does not retry a message we can never read.
        return Response(status_code=204)

    session = await _session_service.create_session(
        app_name=_APP_NAME, user_id="assessor"
    )
    invocation_id = str(uuid.uuid4())
    try:
        async for event in _assessor_runner.run_async(
            user_id="assessor",
            session_id=session.id,
            invocation_id=invocation_id,
            new_message=types.Content(
                role="user",
                parts=[types.Part(text=json.dumps({"claim_id": claim_id}))],
            ),
        ):
            logger.info(
                "claim_opened.event",
                extra={"claim_id": claim_id, "author": event.author},
            )
    except Exception:
        logger.exception("claim_opened.failed", extra={"claim_id": claim_id})
        # Non-2xx so Pub/Sub retries; run_assessment's create-if-absent
        # guard on `findings` makes a retry safe.
        return Response(status_code=500)

    return Response(status_code=204)
