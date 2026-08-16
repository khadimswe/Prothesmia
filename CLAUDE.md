# CLAUDE.md

> Read this file first, every run, before touching any issue. It defines what
> Prothesmia is and what it is not. If an issue conflicts with this file, the
> issue is wrong — open a `needs-human` issue instead of guessing.

**Project:** Prothesmia — Greek, *the appointed time*: the deadline fixed by statute,
after which a right lapses.

**Submission:** All Things Agentic Hackathon (Google × Devpost), Taskmaster track.
**Deadline:** 2026-08-31 17:00 PDT. **We submit 2026-08-30.**

---

## 1. What this is, in one line

An agent that tracks the statutory deadline on a stalled insurance claim and
autonomously escalates to the state insurance regulator when the carrier blows it.

## 2. The three rules that override everything

### Rule 1 — Never invent a fact

Never invent a statute number, a deadline duration, a FEMA declaration number, an
imagery capture date, a parcel ID, or a dollar figure. Not in code, not in tests,
not in README copy, not in UI placeholder text.

If a value is needed and not verified in this repo, write `TODO(verify)` and open a
`needs-human` issue. A plausible-looking fake statute citation is the single fastest
way to lose this submission.

Verified facts live in §6 of this file and in `docs/SPEC.md`. Use those. Do not
"improve" them.

### Rule 2 — Models extract, rules decide

Gemini reads messy documents — aerial imagery, parcel records, policy PDFs. It
produces structured, human-reviewable extraction with provenance.

A deterministic, versioned, citable rules module does the arithmetic and renders the
verdict. Statutory deadlines, tolling, days remaining, breach determination. Pure
functions. Unit tested against golden cases. Every output carries a rule ID and a
statute citation.

**The Clock and the statute module never call an LLM.** If a task appears to require
it, the task is wrong — open `needs-human`.

Why: "an AI thinks your carrier is 12 days late" is not something a survivor can take
to a regulator. "Notice received 2026-06-25; Fla. Stat. §627.70131(7)(a) requires
payment or denial within 60 days; deadline 2026-08-24; 12 days elapsed past deadline"
is.

### Rule 3 — The agent assembles and escalates. It never asserts a valuation.

Prothesmia does not say "your home sustained $84,000 in damage." It says: here is the
imagery, captured on this date from this source; here is what the model observed in
it; here is the parcel record; here is the declaration; here is the statutory deadline
and the arithmetic; here is a drafted complaint citing the statute.

Output is **computation plus citation**, never a legal or valuation conclusion.
Unauthorized-practice and bad-faith-valuation exposure are both real, and the
assembled-evidence framing is also more credible to a judge.

## 3. What Prothesmia is NOT

Non-negotiable. Violating any of these fails the project regardless of code quality.

- **Not a chatbot.** No "ask Prothesmia about your claim" surface. The agent initiates.
  If a feature requires the user to open the app and ask a question, it is out of scope.
- **Not an insurer-side tool.** Never analyze data to help a carrier defend against a
  claimant. The user is the survivor, always.
- **Not a settlement automator.** It produces a documented packet and a drafted
  complaint for human approval. It does not negotiate or settle.
- **Not a damage estimator.** See Rule 3.
- **Not general-purpose.** No "also track your FEMA IA application." No creep.

## 4. Repo rules for agents

- Never commit to `main`. Open a PR. PRs under ~400 lines.
- Never modify `.github/workflows/`.
- Never touch, print, or commit secrets. No credentials in code or tests.
- Never delete a test to make CI pass.
- Every statute rule function carries a citation string and a golden test. No rule
  merges without one.
- Mark anything unbuilt as `(not built yet)` in README and UI copy. Never fake a
  status table.
- **No PII in logs.** Addresses, carrier names, claimant names, and dollar figures are
  redacted in structured logs. `claim_id` is the correlation key.
- When blocked on a product decision, open `needs-human`. Do not invent scope.

## 5. Stack — hackathon requirements, non-negotiable

| Requirement | Our answer |
|---|---|
| Gemini 3.5+ via Vertex AI | `gemini-3.5-flash`, multimodal imagery assessment |
| Google agent framework | **Google ADK (Python)** — this is the runtime |
| Google Cloud service | Cloud Run, Firestore, Pub/Sub, Cloud Scheduler, Cloud Storage, Secret Manager |
| Hosted URL | Cloud Run |
| Spin-up instructions | `README.md`, written as we go |
| Architecture diagram | Mermaid in `docs/ARCHITECTURE.md` |
| Demo video ~4 min | Must visibly show Cloud Console / Cloud Run / Vertex logs |

Claude Code is the *coding tool*. ADK is the *runtime*. Different layers. Any
non-Google agent framework as the core runtime is disqualifying.

**Bonus, nearly free:** a second Google model (Gemma for a cheap triage/classification
pass), a public build post stating it was written for this hackathon, and a social post
with `#AllThingsAgenticHackathon`.

## 6. Verified facts — use these exact values

### Live infrastructure

```
GCP project ID:      prothesmia
GCP project number:  125318131847
Region:              us-central1
Firestore:           (default), FIRESTORE_NATIVE, nam5
Pub/Sub topics:      clock.tick · clock.breach · clock.tick.dlq
Service account:     prothesmia-agents@prothesmia.iam.gserviceaccount.com
                     roles: datastore.user, pubsub.publisher, aiplatform.user
Auth from CI:        Workload Identity Federation, pool `github`,
                     scoped to repo khadimswe/Prothesmia. No long-lived keys.
```

### Anchor event

```
FEMA-4834-DR-FL
Incident:          Hurricane Milton
Declaration date:  2024-10-11
Incident period:   2024-10-05 to 2024-11-02
State:             Florida
NOAA NGS imagery:  https://storms.ngs.noaa.gov/storms/milton/index.html
```

### Statutes — Fla. Stat. §627.70131. Do not alter these values.

| rule_id | citation | days | trigger | duty |
|---|---|---|---|---|
| `FL-627.70131-1a` | Fla. Stat. §627.70131(1)(a) | 7 | insurer receives a communication about the claim | review and acknowledge receipt |
| `FL-627.70131-7a` | Fla. Stat. §627.70131(7)(a) | 60 | insurer receives notice of the claim | pay or deny, in whole or in part |
| `FL-627.70131-7a-v2021` | Fla. Stat. §627.70131(7)(a) (2021) | 90 | insurer receives notice of the claim | **superseded** by `FL-627.70131-7a` |

The 2021 version is retained deliberately, not deleted. The module must be able to
evaluate a claim under the law in force at the time of loss. The pre-2022 durations
were 90 days to pay-or-deny and 14 days to acknowledge; many secondary sources still
cite the "90-day rule." Ours does not.

**Tolling is mandatory, not optional.** §627.70131 pauses the clock when the insurer
requests information from the policyholder, but only where that request was sent at
least 15 days before the deadline. The Florida Office of Insurance Regulation may
also grant up to 30 additional days. Claims carry `tolled_days`; the arithmetic must
respect it. Asserting a breach that is actually tolled is the most damaging output
this product can produce.

Late payment accrues interest from the date of notice — not from day 61 — per
§627.70131(7)(a), at the rate set by §55.03.

## 7. Honesty constraint — say this plainly, everywhere

The claim files in the demo are **constructed**. The FEMA declaration, the NOAA
imagery, the parcel records, and the statutes are **real**. Every `clock_checks`
record is a **real execution** at a real timestamp.

- `claims` documents carry `constructed: true`, surfaced in the UI.
- The README says it in plain language.
- The demo video says it out loud, once, early.

Never imply a real claimant exists. Never fabricate a survivor's story. A judge
forgives constructed fixtures instantly when they're disclosed and stops trusting
everything else when they aren't.

Likewise: if state DOI complaints turn out to be web-form only rather than
machine-filable, the honest framing is *the agent drafts the complete complaint,
cites the statute, and puts it one click from filing*. That still wins. Overclaiming
automated filing when it isn't wired is the one thing that would sink this.

## 8. Where things live

```
agents/          Python, Google ADK. All five agents. pytest.
  statutes/      Deterministic rules module. NEVER calls an LLM.
web/             Next.js, deployed to Cloud Run (not Vercel).
  styles/tokens.css   The ONLY file permitted to define raw color values.
docs/
  SPEC.md        Product + architecture. Read before any feature work.
  MILESTONES.md  Dated plan. Update status; never add milestones.
  ARCHITECTURE.md  Mermaid diagram.
  ANCHOR.md      The verified declaration + imagery facts.
scripts/         seed.py and deploy helpers.
```

## 9. Design system — binding, see `docs/SPEC.md` §7

Any agent building UI follows it exactly. Deviation is a defect. CI fails the build on
raw hex, gradients, `backdrop-filter`, `blur()`, or fully-rounded pills anywhere under
`web/` outside `tokens.css`. That gate is not negotiable and is not to be weakened to
make a PR pass.
