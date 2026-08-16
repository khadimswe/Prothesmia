"""Golden tests for the statute rules module.

Every rule in `statutes.rules.RULES` is covered here, including the
superseded 90-day rule (retained deliberately, not deleted — CLAUDE.md §6)
and a tolled case. These are golden cases: the expected values are worked
out by hand against the citations in CLAUDE.md §6, not derived from the
implementation.
"""

from datetime import date

import pytest

from statutes.rules import RULES, compute_deadline, evaluate_claim, get_rule


def test_get_rule_unknown_id_raises():
    with pytest.raises(KeyError):
        get_rule("FL-not-a-real-rule")


def test_all_rules_carry_a_citation_and_positive_duration():
    for rule in RULES.values():
        assert rule.citation.startswith("Fla. Stat. §627.70131")
        assert rule.days > 0


# --- FL-627.70131-1a: 7 days to acknowledge -------------------------------


def test_fl_627_70131_1a_deadline_is_seven_days_out():
    deadline = compute_deadline(date(2026, 8, 13), "FL-627.70131-1a")
    assert deadline == date(2026, 8, 20)


def test_fl_627_70131_1a_on_deadline_day_not_yet_breached():
    result = evaluate_claim(
        notice_at=date(2026, 8, 13),
        rule_id="FL-627.70131-1a",
        as_of=date(2026, 8, 20),
    )
    assert result.deadline_at == date(2026, 8, 20)
    assert result.days_remaining == 0
    assert result.breached is False


def test_fl_627_70131_1a_day_after_deadline_is_breached():
    result = evaluate_claim(
        notice_at=date(2026, 8, 13),
        rule_id="FL-627.70131-1a",
        as_of=date(2026, 8, 21),
    )
    assert result.breached is True
    assert result.days_remaining == -1
    assert result.citation == "Fla. Stat. §627.70131(1)(a)"


# --- FL-627.70131-7a: 60 days to pay or deny -------------------------------


def test_fl_627_70131_7a_deadline_is_sixty_days_out():
    deadline = compute_deadline(date(2026, 6, 25), "FL-627.70131-7a")
    assert deadline == date(2026, 8, 24)


def test_fl_627_70131_7a_breached_four_days_past_deadline():
    result = evaluate_claim(
        notice_at=date(2026, 6, 25),
        rule_id="FL-627.70131-7a",
        as_of=date(2026, 8, 28),
    )
    assert result.deadline_at == date(2026, 8, 24)
    assert result.breached is True
    assert result.days_remaining == -4
    assert result.status == "breached"
    assert result.citation == "Fla. Stat. §627.70131(7)(a)"


def test_fl_627_70131_7a_inside_25_percent_window():
    # notice 2026-07-14 -> deadline 2026-09-12 (60 days). As of 2026-08-28,
    # 15 days remain, which is exactly the 25% threshold (0.25 * 60 = 15).
    result = evaluate_claim(
        notice_at=date(2026, 7, 14),
        rule_id="FL-627.70131-7a",
        as_of=date(2026, 8, 28),
    )
    assert result.deadline_at == date(2026, 9, 12)
    assert result.days_remaining == 15
    assert result.breached is False
    assert result.status == "inside_25"


def test_fl_627_70131_7a_comfortable():
    # notice 2026-08-01 -> deadline 2026-09-30 (60 days). As of 2026-08-28,
    # 33 days remain — comfortably above the 15-day threshold.
    result = evaluate_claim(
        notice_at=date(2026, 8, 1),
        rule_id="FL-627.70131-7a",
        as_of=date(2026, 8, 28),
    )
    assert result.deadline_at == date(2026, 9, 30)
    assert result.days_remaining == 33
    assert result.status == "comfortable"


# --- FL-627.70131-7a-v2021: superseded 90-day rule -------------------------


def test_fl_627_70131_7a_v2021_is_marked_superseded():
    rule = get_rule("FL-627.70131-7a-v2021")
    assert rule.superseded_by == "FL-627.70131-7a"
    assert rule.days == 90
    assert rule.citation == "Fla. Stat. §627.70131(7)(a) (2021)"


def test_fl_627_70131_7a_v2021_still_computable_for_pre_2022_losses():
    # A loss governed by the pre-2022 law still needs correct arithmetic —
    # the module must be able to evaluate a claim under the law in force at
    # the time of loss (CLAUDE.md §6).
    result = evaluate_claim(
        notice_at=date(2021, 1, 1),
        rule_id="FL-627.70131-7a-v2021",
        as_of=date(2021, 4, 1),
    )
    assert result.deadline_at == date(2021, 4, 1)  # 90 days out
    assert result.breached is False
    assert result.days_remaining == 0


# --- Tolling: mandatory, not optional --------------------------------------


def test_tolling_pushes_the_deadline_back_by_tolled_days():
    baseline = compute_deadline(date(2026, 6, 1), "FL-627.70131-7a")
    tolled = compute_deadline(
        date(2026, 6, 1), "FL-627.70131-7a", tolled_days=20
    )
    assert baseline == date(2026, 7, 31)  # 60 days, no tolling
    assert tolled == date(2026, 8, 20)  # 60 + 20 days
    assert tolled > baseline


def test_tolled_case_not_breached_when_untolled_deadline_would_have_passed():
    # Without tolling, notice 2026-06-01 + 60 days = 2026-07-31, which would
    # already be breached as of 2026-08-10. With 20 tolled days, the
    # deadline moves to 2026-08-20 and the claim is still open. Asserting a
    # breach that is actually tolled is the most damaging output this
    # product can produce (CLAUDE.md §6) — this is the test that guards it.
    as_of = date(2026, 8, 10)

    untolled = evaluate_claim(
        notice_at=date(2026, 6, 1),
        rule_id="FL-627.70131-7a",
        as_of=as_of,
        tolled_days=0,
    )
    assert untolled.breached is True

    tolled = evaluate_claim(
        notice_at=date(2026, 6, 1),
        rule_id="FL-627.70131-7a",
        as_of=as_of,
        tolled_days=20,
    )
    assert tolled.deadline_at == date(2026, 8, 20)
    assert tolled.breached is False
    assert tolled.days_remaining == 10
    assert tolled.status == "inside_25"  # 10 <= 0.25 * (60 + 20) = 20


def test_negative_tolled_days_rejected():
    with pytest.raises(ValueError):
        compute_deadline(date(2026, 6, 1), "FL-627.70131-7a", tolled_days=-1)


# --- Seed claims (docs/Milestones M0 table) --------------------------------
# These pin the exact seed dates to their expected state as of 2026-08-28,
# so a change to the rules module that breaks the demo's seed data fails
# here first.


@pytest.mark.parametrize(
    "claim_id,notice_at,rule_id,expected_deadline,expected_status",
    [
        (
            "clm-001",
            date(2026, 6, 25),
            "FL-627.70131-7a",
            date(2026, 8, 24),
            "breached",
        ),
        (
            "clm-002",
            date(2026, 8, 13),
            "FL-627.70131-1a",
            date(2026, 8, 20),
            "breached",
        ),
        (
            "clm-003",
            date(2026, 7, 14),
            "FL-627.70131-7a",
            date(2026, 9, 12),
            "inside_25",
        ),
        (
            "clm-004",
            date(2026, 8, 1),
            "FL-627.70131-7a",
            date(2026, 9, 30),
            "comfortable",
        ),
    ],
)
def test_seed_claims_match_milestones_m0_table(
    claim_id, notice_at, rule_id, expected_deadline, expected_status
):
    result = evaluate_claim(
        notice_at=notice_at, rule_id=rule_id, as_of=date(2026, 8, 28)
    )
    assert result.deadline_at == expected_deadline, claim_id
    assert result.status == expected_status, claim_id
