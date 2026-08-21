"""Deterministic Assessor orchestration, with one multimodal call to Gemini.

This module is the only place the Assessor touches Firestore and Cloud
Storage, and the only place in this agent that calls a model. Every fact
that reaches the model — county, parcel identifier, FEMA declaration
number, and each chip's real capture date — is read from Firestore or the
imagery client before the model is ever called. The model's only job is to
describe what is visible in the imagery (CLAUDE.md Rule 2); it never
computes or invents a capture date, a source URL, or a dollar figure
(Rule 3). Clients are passed as arguments (never constructed here) so tests
can pass fakes with no network access and no GCP or Vertex AI credential.

What reaches the model is a *chip*: a parcel-sized crop window-read out of a
multi-megabyte source COG (see `assessor.imagery`), never the COG itself.
That makes provenance a two-level fact, and both levels are recorded here.
Every finding stores the artifact IDs it was drawn from; every artifact
stores the chip's own hash, size, and `gs://` URI *and* the source reference
it was derived from — the source COG URL, its CRS, the exact pixel window
read, the decimation, and the sha256 of the raw window pixels before
encoding. That last value is reproducible from the source and the window
alone, so a reviewer can re-derive the chip and confirm it was not altered.
The same reference is written beside the chip in Cloud Storage as a small
`.provenance.json` manifest, so the stored object is self-describing to
someone holding only the bucket.

Imagery content is validated before it is ever persisted or shown to the
model — a WAF challenge or error page served with HTTP 200 must never land
in `artifacts` as if it were imagery.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

from google.api_core.exceptions import AlreadyExists
from google.genai import types

from assessor.imagery import CHIP_MAX_BYTES, ImageryChip

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

# Chips only. A GeoTIFF is a legitimate *source* here but is never what we
# store or send inline — `assessor.imagery` always encodes a small JPEG/PNG
# chip — so TIFF magic is deliberately not accepted below.
_MAGIC_BYTES_BY_CONTENT_TYPE = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/webp": (b"RIFF",),  # RIFF....WEBP
}

_EXTENSION_BY_CONTENT_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
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


def _validate_image_content(chip: ImageryChip) -> None:
    """Validate content, not status codes (CLAUDE.md §8 / docs/SPEC.md §8).

    An HTML error page or a WAF challenge served as HTTP 200 must never be
    mistaken for imagery and land in `artifacts`. The chip's declared
    content-type must be one the model accepts inline *and* must match the
    payload's actual magic bytes — a mislabelled payload is as bad as a
    fabricated one.
    """
    content = chip.content
    if not content or len(content) < 16:
        raise InvalidImageryContentError(
            f"{chip.label} imagery payload is empty or too small "
            f"({len(content)} bytes) from {chip.source_url}"
        )

    head = content[:512].lstrip().lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html") or b"<html" in head[:64]:
        raise InvalidImageryContentError(
            f"{chip.label} imagery response looks like an HTML page, not "
            f"imagery, from {chip.source_url} (WAF challenge or error page?)"
        )

    content_type = _base_content_type(chip.content_type)
    magic_bytes = _MAGIC_BYTES_BY_CONTENT_TYPE.get(content_type)
    if magic_bytes is None:
        raise InvalidImageryContentError(
            f"{chip.label} chip has content-type {chip.content_type!r}, "
            f"which is not an inline-sendable chip format "
            f"{sorted(_MAGIC_BYTES_BY_CONTENT_TYPE)}, from {chip.source_url}"
        )

    if not any(content.startswith(magic) for magic in magic_bytes):
        raise InvalidImageryContentError(
            f"{chip.label} chip payload does not match the magic bytes of "
            f"its declared type {content_type!r}, from {chip.source_url}"
        )

    if len(content) > CHIP_MAX_BYTES:
        raise InvalidImageryContentError(
            f"{chip.label} chip is {len(content)} bytes, over the "
            f"{CHIP_MAX_BYTES}-byte inline ceiling, from {chip.source_url}"
        )


def _base_content_type(content_type: str) -> str:
    """Strip parameters — a content-type may arrive as e.g.
    `image/tiff; application=geotiff; profile=cloud-optimized` (verified via
    live HEAD request against a Maxar `visual` asset)."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def _extension_for(content_type: str) -> str:
    return _EXTENSION_BY_CONTENT_TYPE.get(_base_content_type(content_type), "bin")


def _source_reference(chip: ImageryChip) -> dict:
    """The exact source asset and window a chip was derived from.

    Stored verbatim in both the Firestore artifact and the Cloud Storage
    `.provenance.json` manifest, so the two never drift. Everything here
    comes from the imagery client's own read — nothing is inferred.
    `window_sha256` is the hash of the raw pixels read out of the window
    before encoding, which is what makes the chip re-derivable.
    """
    col_off, row_off, width, height = chip.source_window
    bands, read_height, read_width = chip.read_shape
    return {
        "asset_url": chip.source_url,
        "crs": chip.source_crs,
        "requested_bbox": list(chip.requested_bbox),
        "chip_bounds": list(chip.chip_bounds),
        "window": {
            "col_off": col_off,
            "row_off": row_off,
            "width": width,
            "height": height,
        },
        "decimation": chip.decimation,
        "read_shape": {
            "bands": bands,
            "height": read_height,
            "width": read_width,
        },
        "read_dtype": "uint8",
        "read_bytes": chip.read_bytes,
        "window_sha256": chip.window_sha256,
    }


def _build_prompt(
    *,
    county: str,
    parcel_id: str,
    fema_declaration: str,
    pre_capture_date: date,
    post_capture_date: date,
) -> str:
    return (
        "You are looking at two aerial imagery crops of the same parcel: "
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

    pre_chip, post_chip = imagery_client.fetch_pair(bbox)
    _validate_image_content(pre_chip)
    _validate_image_content(post_chip)

    artifact_ids = []
    fetched_at = datetime.now(timezone.utc)
    bucket = storage_client.bucket(bucket_name)
    for chip in (pre_chip, post_chip):
        artifact_id = f"{claim_id}:{chip.label}"
        blob_path = f"assessor/{claim_id}/{chip.label}.{_extension_for(chip.content_type)}"
        gcs_uri = f"gs://{bucket_name}/{blob_path}"
        blob = bucket.blob(blob_path)
        blob.upload_from_string(chip.content, content_type=chip.content_type)

        source = _source_reference(chip)
        manifest_path = f"assessor/{claim_id}/{chip.label}.provenance.json"
        manifest = {
            "claim_id": claim_id,
            "artifact_id": artifact_id,
            "label": chip.label,
            "capture_date": chip.capture_date.isoformat(),
            "chip": {
                "gcs_uri": gcs_uri,
                "content_type": chip.content_type,
                "sha256": chip.sha256,
                "bytes": len(chip.content),
            },
            "source": source,
            "fetched_at": fetched_at.isoformat(),
        }
        bucket.blob(manifest_path).upload_from_string(
            json.dumps(manifest, indent=2, sort_keys=True),
            content_type="application/json",
        )

        artifact_ref = db.collection(ARTIFACTS_COLLECTION).document(artifact_id)
        artifact_payload = {
            "claim_id": claim_id,
            "label": chip.label,
            "capture_date": _as_midnight_utc(chip.capture_date),
            # The COG this chip was cropped out of — kept at the top level
            # under its established name so existing readers keep working.
            "source_url": chip.source_url,
            "source": source,
            "gcs_uri": gcs_uri,
            "provenance_gcs_uri": f"gs://{bucket_name}/{manifest_path}",
            "content_type": chip.content_type,
            "sha256": chip.sha256,
            "bytes": len(chip.content),
            "fetched_at": fetched_at,
        }
        try:
            artifact_ref.create(artifact_payload)
        except AlreadyExists:
            logger.info(
                "assessor.artifact_already_present",
                extra={"claim_id": claim_id, "label": chip.label},
            )
        artifact_ids.append(artifact_id)

    prompt = _build_prompt(
        county=county,
        parcel_id=parcel_id,
        fema_declaration=fema_declaration,
        pre_capture_date=pre_chip.capture_date,
        post_capture_date=post_chip.capture_date,
    )
    response = genai_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=pre_chip.content, mime_type=pre_chip.content_type),
            types.Part.from_bytes(data=post_chip.content, mime_type=post_chip.content_type),
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
