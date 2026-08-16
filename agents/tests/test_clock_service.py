"""Tests for clock.service.run_clock_tick against in-memory fakes.

Covers the three idempotency guarantees the Clock must hold under Pub/Sub's
at-least-once delivery: no duplicate `clock_checks` row for the same day, no
duplicate breach escalation, and claims with bad/missing data are skipped
rather than crashing the whole tick.
"""

from datetime import date, datetime, timezone

from clock.service import run_clock_tick
from tests.fakes import FakeFirestoreClient, FakePublisherClient

PROJECT_ID = "prothesmia-test"


def _seed_claim(db: FakeFirestoreClient, claim_id: str, **fields) -> None:
    db.collections.setdefault("claims", {})[claim_id] = {
        "status": "open",
        "tolled_days": 0,
        **fields,
    }


def test_open_claim_gets_one_clock_check_row():
    db = FakeFirestoreClient()
    publisher = FakePublisherClient()
    _seed_claim(
        db,
        "clm-004",
        rule_id="FL-627.70131-7a",
        notice_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    summary = run_clock_tick(
        db, publisher, PROJECT_ID, as_of=date(2026, 8, 28)
    )

    assert summary.claims_read == 1
    assert summary.clock_checks_written == 1
    assert summary.breaches_newly_created == 0
    row = db.collections["clock_checks"]["clm-004:2026-08-28"]
    assert row["status"] == "comfortable"
    assert row["breached"] is False
    assert publisher.published == []


def test_running_twice_for_the_same_day_writes_no_duplicate_row():
    db = FakeFirestoreClient()
    publisher = FakePublisherClient()
    _seed_claim(
        db,
        "clm-004",
        rule_id="FL-627.70131-7a",
        notice_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    run_clock_tick(db, publisher, PROJECT_ID, as_of=date(2026, 8, 28))
    second = run_clock_tick(db, publisher, PROJECT_ID, as_of=date(2026, 8, 28))

    assert second.clock_checks_written == 0
    assert second.clock_checks_already_present == 1
    assert len(db.collections["clock_checks"]) == 1


def test_breach_publishes_exactly_once_across_redelivery():
    db = FakeFirestoreClient()
    publisher = FakePublisherClient()
    _seed_claim(
        db,
        "clm-001",
        rule_id="FL-627.70131-7a",
        notice_at=datetime(2026, 6, 25, tzinfo=timezone.utc),
    )

    first = run_clock_tick(db, publisher, PROJECT_ID, as_of=date(2026, 8, 28))
    second = run_clock_tick(db, publisher, PROJECT_ID, as_of=date(2026, 8, 28))

    assert first.breaches_newly_created == 1
    assert second.breaches_newly_created == 0
    assert len(publisher.published) == 1
    topic_path, payload, attrs = publisher.published[0]
    assert topic_path == f"projects/{PROJECT_ID}/topics/clock.breach"
    assert attrs["claim_id"] == "clm-001"
    assert b"FL-627.70131-7a" in payload


def test_tolled_claim_is_not_breached_when_it_would_be_without_tolling():
    db = FakeFirestoreClient()
    publisher = FakePublisherClient()
    _seed_claim(
        db,
        "clm-tolled",
        rule_id="FL-627.70131-7a",
        notice_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        tolled_days=20,
    )

    run_clock_tick(db, publisher, PROJECT_ID, as_of=date(2026, 8, 10))

    row = db.collections["clock_checks"]["clm-tolled:2026-08-10"]
    assert row["breached"] is False
    assert row["status"] == "inside_25"
    assert publisher.published == []


def test_claim_missing_rule_id_is_skipped_not_fatal():
    db = FakeFirestoreClient()
    publisher = FakePublisherClient()
    _seed_claim(db, "clm-bad", notice_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
    _seed_claim(
        db,
        "clm-004",
        rule_id="FL-627.70131-7a",
        notice_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    summary = run_clock_tick(
        db, publisher, PROJECT_ID, as_of=date(2026, 8, 28)
    )

    assert summary.claims_skipped == 1
    assert "clm-bad" in summary.skipped_claim_ids
    assert summary.clock_checks_written == 1
    assert "clock_checks" not in db.collections or all(
        not doc_id.startswith("clm-bad:")
        for doc_id in db.collections.get("clock_checks", {})
    )


def test_closed_claim_is_not_read():
    db = FakeFirestoreClient()
    publisher = FakePublisherClient()
    _seed_claim(
        db,
        "clm-closed",
        status="closed",
        rule_id="FL-627.70131-7a",
        notice_at=datetime(2026, 6, 25, tzinfo=timezone.utc),
    )

    summary = run_clock_tick(
        db, publisher, PROJECT_ID, as_of=date(2026, 8, 28)
    )

    assert summary.claims_read == 0
    assert publisher.published == []
