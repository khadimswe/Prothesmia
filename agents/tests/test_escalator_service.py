"""Tests for escalator.service.run_escalation against in-memory fakes.

Covers: the drafted complaint carries the exact statute citation and the
other four sourced facts; no currency figure ever survives into a stored
complaint (CLAUDE.md Rule 3); a redelivered breach drafts — and calls
Gemini — exactly once; and the claim's status flips to escalated only after
a successful draft.
"""

import re
from datetime import datetime, timezone

import pytest

from escalator.service import run_escalation
from statutes.rules import get_rule
from tests.fakes import FakeFirestoreClient, FakeGenAIClient

CITATION_7A = get_rule("FL-627.70131-7a").citation
DUTY_7A = get_rule("FL-627.70131-7a").duty


def _seed_claim(db: FakeFirestoreClient, claim_id: str, **fields) -> None:
    db.collections.setdefault("claims", {})[claim_id] = {
        "status": "open",
        "tolled_days": 0,
        **fields,
    }


def _seed_breach(db: FakeFirestoreClient, claim_id: str, **fields) -> None:
    db.collections.setdefault("breaches", {})[f"{claim_id}:breach"] = {
        "claim_id": claim_id,
        **fields,
    }


def _clean_draft(citation: str, duty: str) -> str:
    return (
        f"The insurer failed to {duty} within the window required by "
        f"{citation}. Notice was received 2026-06-25; the statutory "
        f"deadline of 2026-08-24 has passed. The Department is asked to "
        f"investigate this non-response."
    )


def test_escalation_drafts_complaint_and_marks_claim_escalated():
    db = FakeFirestoreClient()
    genai_client = FakeGenAIClient(response_text=_clean_draft(CITATION_7A, DUTY_7A))
    _seed_claim(
        db,
        "clm-001",
        rule_id="FL-627.70131-7a",
        notice_at=datetime(2026, 6, 25, tzinfo=timezone.utc),
    )
    _seed_breach(
        db,
        "clm-001",
        rule_id="FL-627.70131-7a",
        citation=CITATION_7A,
        deadline_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        escalated_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    outcome = run_escalation(db, genai_client, "clm-001")

    assert outcome.drafted is True
    assert outcome.citation == CITATION_7A
    row = db.collections["escalations"]["clm-001:escalation"]
    assert row["citation"] == CITATION_7A
    assert CITATION_7A in row["drafted_text"]
    assert row["duty"] == DUTY_7A
    assert row["days_elapsed_past_deadline"] == 4
    assert row["approval_status"] == "pending_human_approval"
    assert "TODO(verify)" in row["filing_note"]
    assert db.collections["claims"]["clm-001"]["status"] == "escalated"
    assert len(genai_client.models.calls) == 1


def test_no_currency_pattern_appears_in_stored_output():
    db = FakeFirestoreClient()
    genai_client = FakeGenAIClient(response_text=_clean_draft(CITATION_7A, DUTY_7A))
    _seed_claim(
        db,
        "clm-002",
        rule_id="FL-627.70131-7a",
        notice_at=datetime(2026, 6, 25, tzinfo=timezone.utc),
    )
    _seed_breach(
        db,
        "clm-002",
        deadline_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        escalated_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    run_escalation(db, genai_client, "clm-002")

    drafted_text = db.collections["escalations"]["clm-002:escalation"]["drafted_text"]
    assert "$" not in drafted_text
    assert not re.search(r"\b(dollars?|usd|cents?)\b", drafted_text, re.IGNORECASE)


def test_drafted_text_with_a_currency_figure_is_rejected():
    db = FakeFirestoreClient()
    genai_client = FakeGenAIClient(
        response_text=(
            f"Per {CITATION_7A}, the insurer owes an estimated $84,000 in "
            f"damages and is 4 days past deadline."
        )
    )
    _seed_claim(
        db,
        "clm-003",
        rule_id="FL-627.70131-7a",
        notice_at=datetime(2026, 6, 25, tzinfo=timezone.utc),
    )
    _seed_breach(
        db,
        "clm-003",
        deadline_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        escalated_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="currency"):
        run_escalation(db, genai_client, "clm-003")

    assert "escalations" not in db.collections or not db.collections["escalations"]
    assert db.collections["claims"]["clm-003"]["status"] == "open"


def test_drafted_text_missing_the_citation_is_rejected():
    db = FakeFirestoreClient()
    genai_client = FakeGenAIClient(
        response_text="The insurer missed the statutory deadline to pay or deny."
    )
    _seed_claim(
        db,
        "clm-004",
        rule_id="FL-627.70131-7a",
        notice_at=datetime(2026, 6, 25, tzinfo=timezone.utc),
    )
    _seed_breach(
        db,
        "clm-004",
        deadline_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        escalated_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="citation"):
        run_escalation(db, genai_client, "clm-004")

    assert "escalations" not in db.collections or not db.collections["escalations"]


def test_redelivered_breach_drafts_and_calls_gemini_exactly_once():
    db = FakeFirestoreClient()
    genai_client = FakeGenAIClient(response_text=_clean_draft(CITATION_7A, DUTY_7A))
    _seed_claim(
        db,
        "clm-005",
        rule_id="FL-627.70131-7a",
        notice_at=datetime(2026, 6, 25, tzinfo=timezone.utc),
    )
    _seed_breach(
        db,
        "clm-005",
        deadline_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        escalated_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    first = run_escalation(db, genai_client, "clm-005")
    second = run_escalation(db, genai_client, "clm-005")

    assert first.drafted is True
    assert second.drafted is False
    assert len(genai_client.models.calls) == 1
    assert len(db.collections["escalations"]) == 1


def test_missing_breach_record_raises():
    db = FakeFirestoreClient()
    genai_client = FakeGenAIClient(response_text=_clean_draft(CITATION_7A, DUTY_7A))
    _seed_claim(
        db,
        "clm-006",
        rule_id="FL-627.70131-7a",
        notice_at=datetime(2026, 6, 25, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="breach record not found"):
        run_escalation(db, genai_client, "clm-006")

    assert len(genai_client.models.calls) == 0
