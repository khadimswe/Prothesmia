"""Deterministic Assessor orchestration, with one multimodal call to Gemini.

This module is the only place the Assessor touches Firestore and Cloud
Storage, and the only place in this agent that calls a model. Every fact
that reaches the model — county, parcel identifier, FEMA declaration
number, and each tile's real capture date — is read from Firestore or the
imagery client before the model is ever called. The model's only job is to
describe what is visible in the imagery (CLAUDE.md Rule 2); it never
computes or invents a capture date, a source URL, or a dollar figure
(Rule 3). Clients are passed as arguments (never constructed here) so tests
can pass fakes with no network access and no GCP or Vertex AI credential.

Provenance is mandatory: every finding stores the artifact IDs it was drawn
from, and every artifact stores the real capture date and source URL the
imagery client returned. Imagery content is validated before it is ever
persisted or shown to the model — a WAF challenge or error page served with
HTTP 200 must never land in `artifacts` as if it were imagery.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

from google.api_core.exceptions import AlreadyExists
from google.genai import types

from assessor.imagery import ImageryTile

logger = logging.getLogger("prothesmia.assessor")

CLAIMS_COLLECTION = "claims"
ARTIFACTS_COLLECTION = "artifacts"
FINDINGS_COLLECTION = "findings"
GEMINI_MODEL = "gemini-3.5-flash"

# Reference only — Assessor is triggered by a push subscription bound to
# this topic; it does not publish to it. Topic/subscription creation is
# handled outside this repo (see agents/main.py docstring).
CLAIM_OPENED_TOPIC = "claim.opened"

_CURRENCY_PATTERNS = [
    re.compile(r"\$\s?\d"),
    re.compile(r"\b\d{1,3}(,\d{3})+(\.\d{2})?\b"),
    re.compile(r"\bUSD\b", re.IGNORECASE),
    re.compile(r"\bdollars?\b", re.IGNORECASE),
    re.compile(r"\bcents?\b", re.IGNORECASE),
]

_IMAGE_MAGIC_BYTES = (
    b"\xff\xd8\xff",  # JPEG
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"II*\x00",  # TIFF, little-endian
    b"MM\x00*",  # TIFF, big-endian
    b"RIFF",  # WEBP (RIFF....WEBP)
)

_EXTENSION_BY_CONTENT_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/tiff": "tif",
    "image/webp": "webp",
}


class InvalidImageryContentError(RuntimeError):
    """Imagery payload did not look like real image content."""


@dataclass
class AssessmentOutcome:
    claim_id: str
    assessed: bool
    insufficient_evidence: bool | None = None
    observation_count: int = 0


def _contains_currency(text: str) -> bool:
    return any(pattern.search(text) for pattern in _CURRENCY_PATTERNS)


def _as_midnight_utc(value: date) -> datetime:
    return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)


def _validate_image_content(tile: ImageryTile) -> None:
    """Validate content, not status codes (CLAUDE.md §8 / docs/SPEC.md §8).

    An HTML error page or a WAF challenge served as HTTP 200 must never be
    mistaken for imagery and land in `artifacts`.
    """
    content = tile.content
    if not content or len(content) < 16:
        raise InvalidImageryContentError(
            f"{tile.label} imagery payload is empty or too small "
            f"({len(content)} bytes) from {tile.source_url}"
        )

    head = content[:512].lstrip().lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html") or b"<html" in head[:64]:
        raise InvalidImageryContentError(
            f"{tile.label} imagery response looks like an HTML page, not "
            f"imagery, from {tile.source_url} (WAF challenge or error page?)"
        )

    content_type = (tile.content_type or "").lower()
    if not content_type.startswith("image/"):
        raise InvalidImageryContentError(
            f"{tile.label} imagery response has non-image content-type "
            f"{content_type!r} from {tile.source_url}"
        )

    if not any(content.startswith(magic) for magic in _IMAGE_MAGIC_BYTES):
        raise InvalidImageryContentError(
            f"{tile.label} imagery payload does not match a known image "
            f"format's magic bytes, from {tile.source_url}"
        )


def _extension_for(content_type: str) -> str:
    return _EXTENSION_BY_CONTENT_TYPE.get((content_type or "").lower(), "bin")


def _build_prompt(
    *,
    county: str,
    parcel_id: str,
    fema_declaration: str,
    pre_capture_date: date,
    post_capture_date: date,
) -> str:
    return (
        "You are looking at two aerial imagery tiles of the same parcel: "
        f"one captured {pre_capture_date.isoformat()} (before Hurricane "
        f"Milton) and one captured {post_capture_date.isoformat()} (after "
        f"Hurricane Milton). The parcel is in {county} County, Florida, "
        f"under FEMA declaration {fema_declaration}, parcel identifier "
        f"{parcel_id}.\n\n"
        "Describe ONLY what is visually observable in the imagery — roof "
        "condition, visible debris, standing water, structural damage to "
        "visible surfaces, changes in vegetation, fencing, or outbuildings, "
        "and similar physical observations. For each observation, state "
        "your confidence as a number from 0 to 1.\n\n"
        "Do NOT state, imply, or estimate any dollar figure, damage "
        "valuation, repair cost, or settlement amount — none should appear "
        "anywhere in your output. You are describing what is visible, not "
        "what it is worth.\n\n"
        "If the imagery is too low-resolution, too obstructed (cloud "
        "cover, shadow, camera angle), or otherwise insufficient to make a "
        "confident observation, set insufficient_evidence to true and "
        "state exactly why in insufficient_evidence_reason, rather than "
        "guessing."
    )


_OBSERVATION_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "feature": types.Schema(type=types.Type.STRING),
        "description": types.Schema(type=types.Type.STRING),
        "confidence": types.Schema(type=types.Type.NUMBER),
    },
    required=["feature", "description", "confidence"],
)

FINDING_RESPONSE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "insufficient_evidence": types.Schema(type=types.Type.BOOLEAN),
        "insufficient_evidence_reason": types.Schema(type=types.Type.STRING),
        "observations": types.Schema(
            type=types.Type.ARRAY,
            items=_OBSERVATION_SCHEMA,
        ),
    },
    required=["insufficient_evidence", "observations"],
)


def _parse_finding_response(raw_text: str) -> tuple[list[dict], bool, str | None]:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"assessor model response was not valid JSON: {exc}"
        ) from exc

    if "insufficient_evidence" not in payload or "observations" not in payload:
        raise ValueError(
            "assessor model response missing required fields "
            "insufficient_evidence/observations"
        )

    insufficient_evidence = bool(payload["insufficient_evidence"])
    reason = payload.get("insufficient_evidence_reason") or None
    observations = payload["observations"]

    if insufficient_evidence and not reason:
        raise ValueError(
            "model flagged insufficient_evidence but gave no "
            "insufficient_evidence_reason"
        )

    all_text = " ".join(
        [reason or ""]
        + [str(obs.get("feature", "")) for obs in observations]
        + [str(obs.get("description", "")) for obs in observations]
    )
    if _contains_currency(all_text):
        raise ValueError(
            "assessor model response contains a currency figure; refusing "
            "to store it (CLAUDE.md Rule 3)"
        )

    for obs in observations:
        confidence = obs.get("confidence")
        if not isinstance(confidence, (int, float)) or not (0 <= confidence <= 1):
            raise ValueError(
                f"observation confidence out of range [0, 1]: {confidence!r}"
            )

    return observations, insufficient_evidence, reason


def run_assessment(
    db,
    storage_client,
    imagery_client,
    genai_client,
    claim_id: str,
    bucket_name: str,
) -> AssessmentOutcome:
    """Fetch parcel imagery, validate it, and write a structured finding.

    Idempotent under Pub/Sub's at-least-once delivery: if `findings/
    {claim_id}:finding` already exists, this returns immediately without
    re-fetching imagery or calling the model a second time.
    """
    finding_ref = db.collection(FINDINGS_COLLECTION).document(f"{claim_id}:finding")
    if finding_ref.get().exists:
        logger.info("assessor.already_assessed", extra={"claim_id": claim_id})
        return AssessmentOutcome(claim_id=claim_id, assessed=False)

    claim_snap = db.collection(CLAIMS_COLLECTION).document(claim_id).get()
    if not claim_snap.exists:
        logger.error("assessor.claim_not_found", extra={"claim_id": claim_id})
        raise ValueError(f"claim not found: {claim_id}")
    claim_data = claim_snap.to_dict()

    county = claim_data.get("county")
    parcel_id = claim_data.get("parcel_id")
    fema_declaration = claim_data.get("fema_declaration")
    imagery_bbox = claim_data.get("imagery_bbox")
    missing = [
        name
        for name, value in (
            ("county", county),
            ("parcel_id", parcel_id),
            ("fema_declaration", fema_declaration),
            ("imagery_bbox", imagery_bbox),
        )
        if not value
    ]
    if missing:
        logger.error("assessor.claim_missing_fields", extra={"claim_id": claim_id})
        raise ValueError(
            f"claim {claim_id} missing required field(s) {missing}; "
            "TODO(verify): parcel resolution (Intake) is not built yet — "
            "these must be set on the claim before the Assessor can run"
        )
    bbox = tuple(imagery_bbox)

    pre_tile, post_tile = imagery_client.fetch_pair(bbox)
    _validate_image_content(pre_tile)
    _validate_image_content(post_tile)

    artifact_ids = []
    for tile in (pre_tile, post_tile):
        artifact_id = f"{claim_id}:{tile.label}"
        blob_path = f"assessor/{claim_id}/{tile.label}.{_extension_for(tile.content_type)}"
        blob = storage_client.bucket(bucket_name).blob(blob_path)
        blob.upload_from_string(tile.content, content_type=tile.content_type)

        artifact_ref = db.collection(ARTIFACTS_COLLECTION).document(artifact_id)
        artifact_payload = {
            "claim_id": claim_id,
            "label": tile.label,
            "capture_date": _as_midnight_utc(tile.capture_date),
            "source_url": tile.source_url,
            "gcs_uri": f"gs://{bucket_name}/{blob_path}",
            "content_type": tile.content_type,
            "sha256": hashlib.sha256(tile.content).hexdigest(),
            "bytes": len(tile.content),
            "fetched_at": datetime.now(timezone.utc),
        }
        try:
            artifact_ref.create(artifact_payload)
        except AlreadyExists:
            logger.info(
                "assessor.artifact_already_present",
                extra={"claim_id": claim_id, "label": tile.label},
            )
        artifact_ids.append(artifact_id)

    prompt = _build_prompt(
        county=county,
        parcel_id=parcel_id,
        fema_declaration=fema_declaration,
        pre_capture_date=pre_tile.capture_date,
        post_capture_date=post_tile.capture_date,
    )
    response = genai_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=pre_tile.content, mime_type=pre_tile.content_type),
            types.Part.from_bytes(data=post_tile.content, mime_type=post_tile.content_type),
            types.Part(text=prompt),
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=FINDING_RESPONSE_SCHEMA,
        ),
    )

    observations, insufficient_evidence, reason = _parse_finding_response(response.text)

    finding_payload = {
        "claim_id": claim_id,
        "artifact_ids": artifact_ids,
        "model": GEMINI_MODEL,
        "observations": observations,
        "insufficient_evidence": insufficient_evidence,
        "insufficient_evidence_reason": reason,
        "assessed_at": datetime.now(timezone.utc),
    }
    try:
        finding_ref.create(finding_payload)
    except AlreadyExists:
        logger.info("assessor.already_assessed", extra={"claim_id": claim_id})
        return AssessmentOutcome(claim_id=claim_id, assessed=False)

    logger.info(
        "assessor.finding_written",
        extra={
            "claim_id": claim_id,
            "insufficient_evidence": insufficient_evidence,
            "observation_count": len(observations),
        },
    )
    return AssessmentOutcome(
        claim_id=claim_id,
        assessed=True,
        insufficient_evidence=insufficient_evidence,
        observation_count=len(observations),
    )
