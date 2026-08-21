"""Tests for assessor.service.run_assessment against in-memory fakes.

Covers: a full assessment stores two provenance-carrying artifacts and one
finding that cites them; the chip's source COG, window, and pre-encoding
window hash are recorded in Firestore and in a Cloud Storage manifest, so
the chip traces back to the exact pixels it came from; only the small chip
— never a source COG — is sent to the model; `insufficient_evidence` is a
first-class stored outcome, not an error; no currency figure ever survives
into a stored finding (CLAUDE.md Rule 3); imagery content is validated
before it is persisted (a WAF/HTML response must never land in
`artifacts`); a redelivered claim.opened assesses — and calls Gemini —
exactly once; and a claim missing the fields the Assessor needs fails
loudly rather than guessing.

No test here touches the network or rasterio: chips are built in memory by
`_chip()` below, the same way the real `assessor.imagery.read_chip` would
return them after a `/vsicurl/` windowed read.
"""

import hashlib
import json
import re
from datetime import date, datetime, timezone

import pytest

from assessor.imagery import CHIP_MAX_BYTES, ImageryChip
from assessor.service import InvalidImageryContentError, run_assessment
from tests.fakes import (
    FakeFirestoreClient,
    FakeGenAIClient,
    FakeImageryClient,
    FakeStorageClient,
)

BUCKET = "prothesmia-artifacts-test"

_JPEG_MAGIC = b"\xff\xd8\xff\xe0" + b"\x00" * 32
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_RAW_WINDOW_PIXELS = bytes(range(256)) * 4


def _seed_claim(db: FakeFirestoreClient, claim_id: str, **fields) -> None:
    db.collections.setdefault("claims", {})[claim_id] = {
        "status": "open",
        "county": "Pinellas",
        "parcel_id": "12-34-56-00000-000-0010",
        "fema_declaration": "FEMA-4834-DR-FL",
        "imagery_bbox": [-82.70, 27.90, -82.69, 27.91],
        **fields,
    }


def _chip(
    label: str,
    capture_date: date,
    content: bytes = _JPEG_MAGIC,
    content_type: str = "image/jpeg",
) -> ImageryChip:
    return ImageryChip(
        label=label,
        capture_date=capture_date,
        source_url=f"https://maxar-opendata.s3.amazonaws.com/{label}.tif",
        source_crs="EPSG:32617",
        requested_bbox=(-82.70, 27.90, -82.69, 27.91),
        chip_bounds=(-82.701, 27.899, -82.689, 27.911),
        source_window=(11771, 12965, 4885, 3700),
        decimation=5,
        read_shape=(3, 740, 977),
        read_bytes=len(_RAW_WINDOW_PIXELS),
        window_sha256=hashlib.sha256(_RAW_WINDOW_PIXELS).hexdigest(),
        content=content,
        content_type=content_type,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _clean_response(observations: list[dict], insufficient_evidence: bool = False, reason: str | None = None) -> str:
    return json.dumps(
        {
            "insufficient_evidence": insufficient_evidence,
            "insufficient_evidence_reason": reason,
            "observations": observations,
        }
    )


def test_assessment_writes_artifacts_and_finding_with_provenance():
    db = FakeFirestoreClient()
    storage = FakeStorageClient()
    imagery = FakeImageryClient(
        pre_chip=_chip("pre_event", date(2024, 9, 20)),
        post_chip=_chip("post_event", date(2024, 10, 12)),
    )
    observations = [
        {"feature": "roof", "description": "visible shingle loss on the north slope", "confidence": 0.82},
    ]
    genai_client = FakeGenAIClient(response_text=_clean_response(observations))
    _seed_claim(db, "clm-101")

    outcome = run_assessment(db, storage, imagery, genai_client, "clm-101", BUCKET)

    assert outcome.assessed is True
    assert outcome.insufficient_evidence is False
    assert outcome.observation_count == 1
    assert imagery.bbox_calls == [(-82.70, 27.90, -82.69, 27.91)]

    pre_artifact = db.collections["artifacts"]["clm-101:pre_event"]
    post_artifact = db.collections["artifacts"]["clm-101:post_event"]
    assert pre_artifact["capture_date"] == datetime(2024, 9, 20, tzinfo=timezone.utc)
    assert pre_artifact["source_url"] == "https://maxar-opendata.s3.amazonaws.com/pre_event.tif"
    assert pre_artifact["gcs_uri"] == f"gs://{BUCKET}/assessor/clm-101/pre_event.jpg"
    assert post_artifact["capture_date"] == datetime(2024, 10, 12, tzinfo=timezone.utc)
    assert storage.buckets[BUCKET].blobs["assessor/clm-101/pre_event.jpg"] == _JPEG_MAGIC

    finding = db.collections["findings"]["clm-101:finding"]
    assert finding["artifact_ids"] == ["clm-101:pre_event", "clm-101:post_event"]
    assert finding["observations"] == observations
    assert finding["insufficient_evidence"] is False
    assert len(genai_client.models.calls) == 1


def test_insufficient_evidence_is_stored_as_a_result_not_an_error():
    db = FakeFirestoreClient()
    storage = FakeStorageClient()
    imagery = FakeImageryClient(
        pre_chip=_chip("pre_event", date(2024, 9, 20)),
        post_chip=_chip("post_event", date(2024, 10, 12)),
    )
    genai_client = FakeGenAIClient(
        response_text=_clean_response(
            [],
            insufficient_evidence=True,
            reason="post-event tile is fully obscured by cloud cover",
        )
    )
    _seed_claim(db, "clm-102")

    outcome = run_assessment(db, storage, imagery, genai_client, "clm-102", BUCKET)

    assert outcome.assessed is True
    assert outcome.insufficient_evidence is True
    finding = db.collections["findings"]["clm-102:finding"]
    assert finding["insufficient_evidence"] is True
    assert finding["insufficient_evidence_reason"] == "post-event tile is fully obscured by cloud cover"
    assert finding["observations"] == []
    # Provenance is still recorded even when the model could not assess.
    assert "clm-102:pre_event" in db.collections["artifacts"]
    assert "clm-102:post_event" in db.collections["artifacts"]


def test_no_currency_pattern_appears_in_any_finding_output():
    db = FakeFirestoreClient()
    storage = FakeStorageClient()
    imagery = FakeImageryClient(
        pre_chip=_chip("pre_event", date(2024, 9, 20)),
        post_chip=_chip("post_event", date(2024, 10, 12)),
    )
    observations = [
        {"feature": "roof", "description": "several missing shingles near the ridge", "confidence": 0.6},
        {"feature": "yard", "description": "downed tree limbs across the driveway", "confidence": 0.75},
    ]
    genai_client = FakeGenAIClient(response_text=_clean_response(observations))
    _seed_claim(db, "clm-103")

    run_assessment(db, storage, imagery, genai_client, "clm-103", BUCKET)

    finding = db.collections["findings"]["clm-103:finding"]
    serialized = json.dumps(finding["observations"])
    assert "$" not in serialized
    assert not re.search(r"\b(dollars?|usd|cents?)\b", serialized, re.IGNORECASE)


def test_drafted_finding_with_a_currency_figure_is_rejected():
    db = FakeFirestoreClient()
    storage = FakeStorageClient()
    imagery = FakeImageryClient(
        pre_chip=_chip("pre_event", date(2024, 9, 20)),
        post_chip=_chip("post_event", date(2024, 10, 12)),
    )
    observations = [
        {"feature": "roof", "description": "damage estimated at $12,000 to repair", "confidence": 0.6},
    ]
    genai_client = FakeGenAIClient(response_text=_clean_response(observations))
    _seed_claim(db, "clm-104")

    with pytest.raises(ValueError, match="currency"):
        run_assessment(db, storage, imagery, genai_client, "clm-104", BUCKET)

    assert "findings" not in db.collections or not db.collections["findings"]


def test_html_error_page_served_as_imagery_is_rejected_before_persisting():
    db = FakeFirestoreClient()
    storage = FakeStorageClient()
    imagery = FakeImageryClient(
        pre_chip=_chip("pre_event", date(2024, 9, 20)),
        post_chip=_chip(
            "post_event",
            date(2024, 10, 12),
            content=b"<!DOCTYPE html><html><body>Access Denied</body></html>",
            content_type="image/jpeg",
        ),
    )
    genai_client = FakeGenAIClient(response_text=_clean_response([]))
    _seed_claim(db, "clm-105")

    with pytest.raises(InvalidImageryContentError):
        run_assessment(db, storage, imagery, genai_client, "clm-105", BUCKET)

    assert "artifacts" not in db.collections or not db.collections["artifacts"]
    assert "findings" not in db.collections or not db.collections["findings"]
    assert len(genai_client.models.calls) == 0


def test_artifact_traces_the_chip_back_to_its_source_cog_and_window():
    db = FakeFirestoreClient()
    storage = FakeStorageClient()
    pre_chip = _chip("pre_event", date(2024, 9, 20))
    imagery = FakeImageryClient(
        pre_chip=pre_chip,
        post_chip=_chip("post_event", date(2024, 10, 12)),
    )
    genai_client = FakeGenAIClient(response_text=_clean_response([]))
    _seed_claim(db, "clm-109")

    run_assessment(db, storage, imagery, genai_client, "clm-109", BUCKET)

    artifact = db.collections["artifacts"]["clm-109:pre_event"]
    source = artifact["source"]
    assert source["asset_url"] == pre_chip.source_url
    assert source["crs"] == "EPSG:32617"
    assert source["window"] == {
        "col_off": 11771,
        "row_off": 12965,
        "width": 4885,
        "height": 3700,
    }
    assert source["decimation"] == 5
    assert source["read_shape"] == {"bands": 3, "height": 740, "width": 977}
    # The pre-encoding window hash is what makes the chip re-derivable; it
    # must be distinct from the hash of the encoded chip itself.
    assert source["window_sha256"] == hashlib.sha256(_RAW_WINDOW_PIXELS).hexdigest()
    assert artifact["sha256"] == hashlib.sha256(_JPEG_MAGIC).hexdigest()
    assert source["window_sha256"] != artifact["sha256"]
    assert artifact["bytes"] == len(_JPEG_MAGIC)
    assert source["requested_bbox"] == [-82.70, 27.90, -82.69, 27.91]
    assert source["chip_bounds"] == [-82.701, 27.899, -82.689, 27.911]


def test_chip_is_stored_beside_a_self_describing_provenance_manifest():
    db = FakeFirestoreClient()
    storage = FakeStorageClient()
    imagery = FakeImageryClient(
        pre_chip=_chip("pre_event", date(2024, 9, 20)),
        post_chip=_chip("post_event", date(2024, 10, 12)),
    )
    genai_client = FakeGenAIClient(response_text=_clean_response([]))
    _seed_claim(db, "clm-110")

    run_assessment(db, storage, imagery, genai_client, "clm-110", BUCKET)

    blobs = storage.buckets[BUCKET].blobs
    manifest_path = "assessor/clm-110/post_event.provenance.json"
    assert manifest_path in blobs
    manifest = json.loads(blobs[manifest_path])
    assert manifest["claim_id"] == "clm-110"
    assert manifest["artifact_id"] == "clm-110:post_event"
    assert manifest["capture_date"] == "2024-10-12"
    assert manifest["chip"]["gcs_uri"] == f"gs://{BUCKET}/assessor/clm-110/post_event.jpg"
    assert manifest["chip"]["sha256"] == hashlib.sha256(_JPEG_MAGIC).hexdigest()

    artifact = db.collections["artifacts"]["clm-110:post_event"]
    assert artifact["provenance_gcs_uri"] == f"gs://{BUCKET}/{manifest_path}"
    # Firestore and the stored manifest must describe the same source.
    assert manifest["source"] == artifact["source"]


def test_only_the_small_chip_is_sent_to_the_model():
    db = FakeFirestoreClient()
    storage = FakeStorageClient()
    imagery = FakeImageryClient(
        pre_chip=_chip("pre_event", date(2024, 9, 20)),
        post_chip=_chip("post_event", date(2024, 10, 12), content=_PNG_MAGIC, content_type="image/png"),
    )
    genai_client = FakeGenAIClient(response_text=_clean_response([]))
    _seed_claim(db, "clm-111")

    run_assessment(db, storage, imagery, genai_client, "clm-111", BUCKET)

    contents = genai_client.models.calls[0]["contents"]
    image_parts = [part for part in contents if part.inline_data is not None]
    assert [part.inline_data.mime_type for part in image_parts] == [
        "image/jpeg",
        "image/png",
    ]
    assert [part.inline_data.data for part in image_parts] == [_JPEG_MAGIC, _PNG_MAGIC]
    assert storage.buckets[BUCKET].blobs["assessor/clm-111/post_event.png"] == _PNG_MAGIC


def test_a_source_geotiff_is_never_accepted_as_a_chip():
    """The source asset is a multi-megabyte COG. If one ever reaches the
    chip path, that is a bug, not something to forward to the model."""
    db = FakeFirestoreClient()
    storage = FakeStorageClient()
    imagery = FakeImageryClient(
        pre_chip=_chip("pre_event", date(2024, 9, 20)),
        post_chip=_chip(
            "post_event",
            date(2024, 10, 12),
            content=b"II*\x00" + b"\x00" * 32,
            content_type="image/tiff; application=geotiff; profile=cloud-optimized",
        ),
    )
    genai_client = FakeGenAIClient(response_text=_clean_response([]))
    _seed_claim(db, "clm-112")

    with pytest.raises(InvalidImageryContentError, match="inline-sendable"):
        run_assessment(db, storage, imagery, genai_client, "clm-112", BUCKET)

    assert "artifacts" not in db.collections or not db.collections["artifacts"]
    assert len(genai_client.models.calls) == 0


def test_chip_whose_bytes_contradict_its_declared_type_is_rejected():
    db = FakeFirestoreClient()
    storage = FakeStorageClient()
    imagery = FakeImageryClient(
        pre_chip=_chip("pre_event", date(2024, 9, 20)),
        post_chip=_chip(
            "post_event", date(2024, 10, 12), content=_JPEG_MAGIC, content_type="image/png"
        ),
    )
    genai_client = FakeGenAIClient(response_text=_clean_response([]))
    _seed_claim(db, "clm-113")

    with pytest.raises(InvalidImageryContentError, match="magic bytes"):
        run_assessment(db, storage, imagery, genai_client, "clm-113", BUCKET)

    assert len(genai_client.models.calls) == 0


def test_chip_over_the_inline_ceiling_is_rejected_before_the_model_call():
    db = FakeFirestoreClient()
    storage = FakeStorageClient()
    oversized = _JPEG_MAGIC + b"\x00" * (CHIP_MAX_BYTES + 1 - len(_JPEG_MAGIC))
    imagery = FakeImageryClient(
        pre_chip=_chip("pre_event", date(2024, 9, 20), content=oversized),
        post_chip=_chip("post_event", date(2024, 10, 12)),
    )
    genai_client = FakeGenAIClient(response_text=_clean_response([]))
    _seed_claim(db, "clm-114")

    with pytest.raises(InvalidImageryContentError, match="inline ceiling"):
        run_assessment(db, storage, imagery, genai_client, "clm-114", BUCKET)

    assert "artifacts" not in db.collections or not db.collections["artifacts"]
    assert len(genai_client.models.calls) == 0


def test_redelivered_claim_opened_assesses_and_calls_gemini_exactly_once():
    db = FakeFirestoreClient()
    storage = FakeStorageClient()
    imagery = FakeImageryClient(
        pre_chip=_chip("pre_event", date(2024, 9, 20)),
        post_chip=_chip("post_event", date(2024, 10, 12)),
    )
    genai_client = FakeGenAIClient(
        response_text=_clean_response(
            [{"feature": "roof", "description": "minor granule loss", "confidence": 0.4}]
        )
    )
    _seed_claim(db, "clm-106")

    first = run_assessment(db, storage, imagery, genai_client, "clm-106", BUCKET)
    second = run_assessment(db, storage, imagery, genai_client, "clm-106", BUCKET)

    assert first.assessed is True
    assert second.assessed is False
    assert len(genai_client.models.calls) == 1
    assert len(imagery.bbox_calls) == 1
    assert len(db.collections["findings"]) == 1


def test_claim_missing_imagery_bbox_raises_without_fetching_imagery():
    db = FakeFirestoreClient()
    storage = FakeStorageClient()
    imagery = FakeImageryClient()
    genai_client = FakeGenAIClient(response_text=_clean_response([]))
    _seed_claim(db, "clm-107", imagery_bbox=None)

    with pytest.raises(ValueError, match="imagery_bbox"):
        run_assessment(db, storage, imagery, genai_client, "clm-107", BUCKET)

    assert imagery.bbox_calls == []
    assert len(genai_client.models.calls) == 0


def test_claim_not_found_raises():
    db = FakeFirestoreClient()
    storage = FakeStorageClient()
    imagery = FakeImageryClient()
    genai_client = FakeGenAIClient(response_text=_clean_response([]))

    with pytest.raises(ValueError, match="claim not found"):
        run_assessment(db, storage, imagery, genai_client, "clm-108", BUCKET)
