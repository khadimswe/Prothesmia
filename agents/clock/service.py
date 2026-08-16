"""Deterministic Clock logic.

This module is the only place the Clock touches Firestore and Pub/Sub. It
takes clients as arguments (never constructs its own) so tests can pass
fakes with no network access and no GCP credential. It never imports
anything model-related — CLAUDE.md Rule 2: the Clock never calls an LLM.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore_v1.base_query import FieldFilter

from statutes.rules import ClockResult, evaluate_claim

logger = logging.getLogger("prothesmia.clock")

CLAIMS_COLLECTION = "claims"
CLOCK_CHECKS_COLLECTION = "clock_checks"
BREACHES_COLLECTION = "breaches"
DEFAULT_BREACH_TOPIC = "clock.breach"


@dataclass
class ClockTickSummary:
    claims_read: int = 0
    claims_skipped: int = 0
    clock_checks_written: int = 0
    clock_checks_already_present: int = 0
    breaches_newly_created: int = 0
    skipped_claim_ids: list[str] = field(default_factory=list)


def _to_date(value: object) -> date:
    """Coerce a Firestore-stored value into a plain calendar date.

    Firestore round-trips timestamp fields as timezone-aware `datetime`.
    Statute arithmetic operates on calendar dates only.
    """
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    raise TypeError(f"expected datetime/date, got {type(value)!r}")


def _evaluate_open_claim(claim_id: str, data: dict, as_of: date) -> ClockResult | None:
    rule_id = data.get("rule_id")
    notice_at_raw = data.get("notice_at")
    if not rule_id or notice_at_raw is None:
        logger.error(
            "clock.claim_missing_fields",
            extra={"claim_id": claim_id},
        )
        return None
    tolled_days = int(data.get("tolled_days", 0) or 0)
    notice_at = _to_date(notice_at_raw)
    try:
        return evaluate_claim(
            notice_at=notice_at,
            rule_id=rule_id,
            as_of=as_of,
            tolled_days=tolled_days,
        )
    except KeyError:
        logger.error(
            "clock.claim_unknown_rule",
            extra={"claim_id": claim_id},
        )
        return None


def _write_clock_check(db, claim_id: str, as_of: date, result: ClockResult) -> bool:
    """Create-if-absent write of clock_checks/{claim_id}:{YYYY-MM-DD}.

    Returns True if this call created the row, False if it already existed
    (a redelivered or manually re-fired tick must not duplicate history).
    """
    doc_id = f"{claim_id}:{as_of.isoformat()}"
    doc_ref = db.collection(CLOCK_CHECKS_COLLECTION).document(doc_id)
    payload = {
        "claim_id": claim_id,
        "date": as_of.isoformat(),
        "rule_id": result.rule_id,
        "citation": result.citation,
        "notice_at": datetime.combine(
            result.notice_at, datetime.min.time(), tzinfo=timezone.utc
        ),
        "tolled_days": result.tolled_days,
        "deadline_at": datetime.combine(
            result.deadline_at, datetime.min.time(), tzinfo=timezone.utc
        ),
        "days_elapsed": result.days_elapsed,
        "days_remaining": result.days_remaining,
        "breached": result.breached,
        "status": result.status,
        "checked_at": datetime.now(timezone.utc),
    }
    try:
        doc_ref.create(payload)
    except AlreadyExists:
        logger.info("clock.clock_check_already_present", extra={"claim_id": claim_id})
        return False
    logger.info("clock.clock_check_written", extra={"claim_id": claim_id})
    return True


def _guard_and_publish_breach(
    db, publisher, project_id: str, topic: str, claim_id: str, result: ClockResult
) -> bool:
    """Create-if-absent breach guard; publish to clock.breach only if new.

    Returns True if this call newly escalated the breach.
    """
    guard_ref = db.collection(BREACHES_COLLECTION).document(f"{claim_id}:breach")
    guard_payload = {
        "claim_id": claim_id,
        "rule_id": result.rule_id,
        "citation": result.citation,
        "deadline_at": datetime.combine(
            result.deadline_at, datetime.min.time(), tzinfo=timezone.utc
        ),
        "escalated_at": datetime.now(timezone.utc),
    }
    try:
        guard_ref.create(guard_payload)
    except AlreadyExists:
        logger.info("clock.breach_already_escalated", extra={"claim_id": claim_id})
        return False

    topic_path = publisher.topic_path(project_id, topic)
    message = {
        "claim_id": claim_id,
        "rule_id": result.rule_id,
        "citation": result.citation,
        "deadline_at": result.deadline_at.isoformat(),
        "days_elapsed": result.days_elapsed,
    }
    publisher.publish(
        topic_path,
        data=json.dumps(message).encode("utf-8"),
        claim_id=claim_id,
    )
    logger.info("clock.breach_published", extra={"claim_id": claim_id})
    return True


def run_clock_tick(
    db,
    publisher,
    project_id: str,
    breach_topic: str = DEFAULT_BREACH_TOPIC,
    as_of: date | None = None,
) -> ClockTickSummary:
    """Evaluate every open claim and record today's clock check.

    Deterministic and idempotent: firing this twice for the same `as_of`
    writes the same `clock_checks` rows once and escalates each breach once.
    """
    as_of = as_of or datetime.now(timezone.utc).date()
    summary = ClockTickSummary()

    query = db.collection(CLAIMS_COLLECTION).where(
        filter=FieldFilter("status", "==", "open")
    )
    for doc in query.stream():
        summary.claims_read += 1
        claim_id = doc.id
        result = _evaluate_open_claim(claim_id, doc.to_dict(), as_of)
        if result is None:
            summary.claims_skipped += 1
            summary.skipped_claim_ids.append(claim_id)
            continue

        if _write_clock_check(db, claim_id, as_of, result):
            summary.clock_checks_written += 1
        else:
            summary.clock_checks_already_present += 1

        if result.breached:
            if _guard_and_publish_breach(
                db, publisher, project_id, breach_topic, claim_id, result
            ):
                summary.breaches_newly_created += 1

    logger.info(
        "clock.tick_complete",
        extra={
            "claims_read": summary.claims_read,
            "claims_skipped": summary.claims_skipped,
            "clock_checks_written": summary.clock_checks_written,
            "clock_checks_already_present": summary.clock_checks_already_present,
            "breaches_newly_created": summary.breaches_newly_created,
        },
    )
    return summary
