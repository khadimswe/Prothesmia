"""The Assessor — pulls parcel imagery and runs the Gemini damage assessment.

Triggered by Pub/Sub `claim.opened`. Fetches pre-event and post-event NOAA
NGS Emergency Response Imagery for the claim's parcel, stores each tile to
Cloud Storage with its real capture date and source URL, and asks Gemini
3.5 Flash multimodal for a structured, confidence-scored description of what
is visible — never a damage valuation (CLAUDE.md Rule 3). A model response
that cannot reach a confident finding is recorded as `insufficient_evidence`,
not treated as an error.
"""
