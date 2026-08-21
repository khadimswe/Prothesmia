#!/usr/bin/env python3
"""Seed the four fixed demo claims into Firestore.

Exact claim IDs, statutes, and dates are pinned in docs/Milestones M0 — do
not alter them here. Idempotent: writes are keyed by claim_id via `.set()`,
so running this twice updates the same four documents in place rather than
duplicating them.

Every seeded claim carries `constructed: true` (CLAUDE.md §7) — these are
constructed demo fixtures, not real claimants. The FEMA declaration, NOAA
imagery, and statutes they reference are real.

Usage:
    python scripts/seed.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

from assessor.fixtures import (  # noqa: E402
    CLM_001_COUNTY,
    CLM_001_FEMA_DECLARATION,
    CLM_001_IMAGERY_BBOX,
)
from common.gcp import get_firestore_client  # noqa: E402
from statutes.rules import compute_deadline  # noqa: E402

# id, rule_id, notice_at, tolled_days — deadline_at is derived, never hand-typed.
SEED_CLAIMS = [
    ("clm-001", "FL-627.70131-7a", date(2026, 6, 25), 0),
    ("clm-002", "FL-627.70131-1a", date(2026, 8, 13), 0),
    ("clm-003", "FL-627.70131-7a", date(2026, 7, 14), 0),
    ("clm-004", "FL-627.70131-7a", date(2026, 8, 1), 0),
]

# clm-001 carries real, verified Maxar imagery reference data (see
# assessor/fixtures.py) so the Assessor can fetch genuine pre/post-event
# imagery instead of a placeholder bbox. `parcel_id` is intentionally not
# set: no real parcel lookup exists yet (Intake milestone, not built), and
# CLAUDE.md Rule 1 forbids inventing one. Until then, run_assessment
# correctly fails loudly on clm-001's missing parcel_id rather than
# guessing.
IMAGERY_FIELDS_BY_CLAIM = {
    "clm-001": {
        "county": CLM_001_COUNTY,
        "fema_declaration": CLM_001_FEMA_DECLARATION,
        "imagery_bbox": list(CLM_001_IMAGERY_BBOX),
    },
}


def _as_utc_datetime(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)


def seed() -> None:
    db = get_firestore_client()
    collection = db.collection("claims")

    for claim_id, rule_id, notice_at, tolled_days in SEED_CLAIMS:
        deadline_at = compute_deadline(notice_at, rule_id, tolled_days)
        collection.document(claim_id).set(
            {
                "claim_id": claim_id,
                "status": "open",
                "rule_id": rule_id,
                "notice_at": _as_utc_datetime(notice_at),
                "tolled_days": tolled_days,
                "deadline_at": _as_utc_datetime(deadline_at),
                "constructed": True,
                **IMAGERY_FIELDS_BY_CLAIM.get(claim_id, {}),
            }
        )
        print(f"seeded {claim_id}: {rule_id}, deadline {deadline_at.isoformat()}")

    print(f"seed complete: {len(SEED_CLAIMS)} claims")


if __name__ == "__main__":
    seed()
