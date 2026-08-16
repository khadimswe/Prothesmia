"""Fla. Stat. §627.70131 — deterministic statute rules.

Pure functions only. No I/O, no LLM calls, no network. Every rule carries a
`rule_id` and a citation string so any downstream output (a clock check, a
breach event, a drafted complaint) can point back to the exact statute
section and duration that produced it.

Values below are transcribed verbatim from CLAUDE.md §6. Do not alter them
here — if a value looks wrong, the fix belongs in CLAUDE.md first, with a
`needs-human` issue explaining why.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

ClaimStatus = Literal["comfortable", "inside_25", "breached"]


@dataclass(frozen=True)
class StatuteRule:
    rule_id: str
    citation: str
    days: int
    trigger: str
    duty: str
    superseded_by: str | None = None


RULES: dict[str, StatuteRule] = {
    "FL-627.70131-1a": StatuteRule(
        rule_id="FL-627.70131-1a",
        citation="Fla. Stat. §627.70131(1)(a)",
        days=7,
        trigger="insurer receives a communication about the claim",
        duty="review and acknowledge receipt",
    ),
    "FL-627.70131-7a": StatuteRule(
        rule_id="FL-627.70131-7a",
        citation="Fla. Stat. §627.70131(7)(a)",
        days=60,
        trigger="insurer receives notice of the claim",
        duty="pay or deny, in whole or in part",
    ),
    "FL-627.70131-7a-v2021": StatuteRule(
        rule_id="FL-627.70131-7a-v2021",
        citation="Fla. Stat. §627.70131(7)(a) (2021)",
        days=90,
        trigger="insurer receives notice of the claim",
        duty="pay or deny, in whole or in part",
        superseded_by="FL-627.70131-7a",
    ),
}


@dataclass(frozen=True)
class ClockResult:
    rule_id: str
    citation: str
    notice_at: date
    tolled_days: int
    deadline_at: date
    as_of: date
    days_elapsed: int
    days_remaining: int
    breached: bool
    status: ClaimStatus


def get_rule(rule_id: str) -> StatuteRule:
    """Look up a statute rule by ID.

    Raises KeyError for anything not in RULES — an unrecognized rule_id must
    fail loudly, never fall back to a guessed duration (CLAUDE.md Rule 1).
    """
    try:
        return RULES[rule_id]
    except KeyError as exc:
        raise KeyError(f"unknown statute rule_id: {rule_id!r}") from exc


def compute_deadline(notice_at: date, rule_id: str, tolled_days: int = 0) -> date:
    """The pay/deny-or-acknowledge deadline for a claim.

    Tolling is mandatory, not optional (CLAUDE.md §6): a claim with
    `tolled_days` set has its deadline pushed back by that many days.
    """
    if tolled_days < 0:
        raise ValueError(f"tolled_days must be >= 0, got {tolled_days}")
    rule = get_rule(rule_id)
    return notice_at + timedelta(days=rule.days + tolled_days)


def evaluate_claim(
    notice_at: date,
    rule_id: str,
    as_of: date,
    tolled_days: int = 0,
) -> ClockResult:
    """Evaluate one claim against its statute as of a given date.

    `days_remaining` is negative once the deadline has passed. A claim is
    `breached` only strictly after its deadline date — the deadline date
    itself is still within the statutory window, not yet a breach.
    """
    rule = get_rule(rule_id)
    deadline_at = compute_deadline(notice_at, rule_id, tolled_days)

    days_elapsed = (as_of - notice_at).days
    days_remaining = (deadline_at - as_of).days
    breached = as_of > deadline_at

    total_window_days = rule.days + tolled_days
    inside_25_threshold = total_window_days * 0.25

    if breached:
        status: ClaimStatus = "breached"
    elif days_remaining <= inside_25_threshold:
        status = "inside_25"
    else:
        status = "comfortable"

    return ClockResult(
        rule_id=rule.rule_id,
        citation=rule.citation,
        notice_at=notice_at,
        tolled_days=tolled_days,
        deadline_at=deadline_at,
        as_of=as_of,
        days_elapsed=days_elapsed,
        days_remaining=days_remaining,
        breached=breached,
        status=status,
    )
