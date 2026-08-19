"""Deterministic Escalator logic, with one drafting call to Gemini.

This module is the only place the Escalator touches Firestore and the only
place in this agent that calls a model. Every fact in the drafted complaint
— the statute citation, its duty text, the notice date, the deadline date,
and days elapsed past deadline — is read from Firestore or looked up in
`statutes.rules` before the model is ever called. The model's only job is to
format those facts into prose (CLAUDE.md Rule 2); it never computes or
invents a date, a citation, or a dollar figure (Rule 3). Clients are passed
as arguments (never constructed here) so tests can pass fakes with no
network access and no GCP or Vertex AI credential.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

from google.api_core.exceptions import AlreadyExists

from statutes.rules import get_rule

logger = logging.getLogger("prothesmia.escalator")

CLAIMS_COLLECTION = "claims"
BREACHES_COLLECTION = "breaches"
ESCALATIONS_COLLECTION = "escalations"
GEMINI_MODEL = "gemini-3.5-flash"

# TODO(verify): confirm whether the Florida Department of Financial Services
# accepts machine-filed complaints or only a web form. Until verified, the
# Escalator only drafts — a human reviews and files by hand.
FILING_NOTE = (
    "TODO(verify): Florida DFS filing channel (machine-filable API vs. "
    "web-form only) has not been confirmed. This complaint is a draft, one "
    "click from filing, pending human review — it is never auto-filed."
)

_CURRENCY_PATTERNS = [
    re.compile(r"\$\s?\d"),
    re.compile(r"\b\d{1,3}(,\d{3})+(\.\d{2})?\b"),
    re.compile(r"\bUSD\b", re.IGNORECASE),
    re.compile(r"\bdollars?\b", re.IGNORECASE),
    re.compile(r"\bcents?\b", re.IGNORECASE),
]


@dataclass
class EscalationOutcome:
    claim_id: str
    drafted: bool
    citation: str | None = None


def _to_date(value: object) -> date:
    """Coerce a Firestore-stored value into a plain calendar date."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    raise TypeError(f"expected datetime/date, got {type(value)!r}")


def _as_midnight_utc(value: date) -> datetime:
    return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)


def _contains_currency(text: str) -> bool:
    return any(pattern.search(text) for pattern in _CURRENCY_PATTERNS)


def _build_prompt(
    *,
    citation: str,
    duty: str,
    notice_at: date,
    deadline_at: date,
    days_elapsed_past_deadline: int,
) -> str:
    return (
        "You are drafting a complaint to the Florida Department of "
        "Financial Services on behalf of a policyholder whose insurer has "
        "missed a statutory deadline on a claim. Format the verified facts "
        "below into a clear, professional complaint narrative of two to "
        "three short paragraphs.\n\n"
        "Use ONLY the facts given below. Do not calculate, estimate, or "
        "invent any date, duration, or citation. Do NOT state, imply, or "
        "estimate any dollar figure, damage valuation, or settlement "
        "amount — none is provided, and none should appear anywhere in "
        "your output.\n\n"
        f"Statute violated: {citation}\n"
        f"Statutory duty: {duty}\n"
        f"Notice of claim received by insurer: {notice_at.isoformat()}\n"
        f"Statutory deadline: {deadline_at.isoformat()}\n"
        f"Days elapsed past deadline as of breach detection: "
        f"{days_elapsed_past_deadline}\n\n"
        "State these facts plainly, identify the insurer's failure to meet "
        "the statutory duty by the deadline, and request that the "
        "Department investigate."
    )


def run_escalation(db, genai_client, claim_id: str) -> EscalationOutcome:
    """Draft a DFS complaint for a breached claim, exactly once.

    Idempotent under Pub/Sub's at-least-once delivery: if `escalations/
    {claim_id}:escalation` already exists, this returns immediately without
    calling the model a second time or re-touching the claim.
    """
    escalation_ref = db.collection(ESCALATIONS_COLLECTION).document(
        f"{claim_id}:escalation"
    )

    if escalation_ref.get().exists:
        logger.info("escalator.already_escalated", extra={"claim_id": claim_id})
        return EscalationOutcome(claim_id=claim_id, drafted=False)

    claim_snap = db.collection(CLAIMS_COLLECTION).document(claim_id).get()
    if not claim_snap.exists:
        logger.error("escalator.claim_not_found", extra={"claim_id": claim_id})
        raise ValueError(f"claim not found: {claim_id}")
    claim_data = claim_snap.to_dict()

    breach_snap = db.collection(BREACHES_COLLECTION).document(
        f"{claim_id}:breach"
    ).get()
    if not breach_snap.exists:
        logger.error("escalator.breach_not_found", extra={"claim_id": claim_id})
        raise ValueError(f"breach record not found: {claim_id}")
    breach_data = breach_snap.to_dict()

    rule_id = claim_data.get("rule_id")
    notice_at_raw = claim_data.get("notice_at")
    if not rule_id or notice_at_raw is None:
        logger.error(
            "escalator.claim_missing_fields", extra={"claim_id": claim_id}
        )
        raise ValueError(f"claim missing rule_id/notice_at: {claim_id}")
    notice_at = _to_date(notice_at_raw)
    rule = get_rule(rule_id)

    deadline_at_raw = breach_data.get("deadline_at")
    escalated_at_raw = breach_data.get("escalated_at")
    if deadline_at_raw is None or escalated_at_raw is None:
        logger.error(
            "escalator.breach_missing_fields", extra={"claim_id": claim_id}
        )
        raise ValueError(f"breach record missing deadline/escalated_at: {claim_id}")
    deadline_at = _to_date(deadline_at_raw)
    escalated_at = _to_date(escalated_at_raw)
    days_elapsed_past_deadline = (escalated_at - deadline_at).days

    prompt = _build_prompt(
        citation=rule.citation,
        duty=rule.duty,
        notice_at=notice_at,
        deadline_at=deadline_at,
        days_elapsed_past_deadline=days_elapsed_past_deadline,
    )
    response = genai_client.models.generate_content(
        model=GEMINI_MODEL, contents=prompt
    )
    drafted_text = response.text

    if _contains_currency(drafted_text):
        logger.error(
            "escalator.drafted_text_rejected_currency",
            extra={"claim_id": claim_id},
        )
        raise ValueError(
            "drafted complaint text contains a currency figure; refusing "
            "to store or file it (CLAUDE.md Rule 3)"
        )
    if rule.citation not in drafted_text:
        logger.error(
            "escalator.drafted_text_missing_citation",
            extra={"claim_id": claim_id},
        )
        raise ValueError(
            "drafted complaint text is missing the statute citation"
        )

    payload = {
        "claim_id": claim_id,
        "rule_id": rule.rule_id,
        "citation": rule.citation,
        "duty": rule.duty,
        "notice_at": _as_midnight_utc(notice_at),
        "deadline_at": _as_midnight_utc(deadline_at),
        "days_elapsed_past_deadline": days_elapsed_past_deadline,
        "drafted_text": drafted_text,
        "model": GEMINI_MODEL,
        "approval_status": "pending_human_approval",
        "filing_note": FILING_NOTE,
        "drafted_at": datetime.now(timezone.utc),
    }
    try:
        escalation_ref.create(payload)
    except AlreadyExists:
        logger.info("escalator.already_escalated", extra={"claim_id": claim_id})
        return EscalationOutcome(claim_id=claim_id, drafted=False)

    db.collection(CLAIMS_COLLECTION).document(claim_id).update(
        {"status": "escalated"}
    )

    logger.info("escalator.escalation_drafted", extra={"claim_id": claim_id})
    return EscalationOutcome(claim_id=claim_id, drafted=True, citation=rule.citation)
