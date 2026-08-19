"""GCP client construction.

Kept separate from business logic so the Clock's decision-making
(`clock/service.py`, `statutes/rules.py`) can be unit tested without a GCP
credential or network access — only this module talks to real clients.
"""

from __future__ import annotations

import os

import google.auth
from google import genai
from google.cloud import firestore, pubsub_v1, storage


def get_project_id() -> str:
    project_id = os.environ.get("GCP_PROJECT_ID") or os.environ.get(
        "GOOGLE_CLOUD_PROJECT"
    )
    if project_id:
        return project_id
    _, project_id = google.auth.default()
    if not project_id:
        raise RuntimeError(
            "no GCP project id: set GCP_PROJECT_ID or run with ADC that "
            "resolves a project"
        )
    return project_id


def get_firestore_client() -> firestore.Client:
    return firestore.Client(project=get_project_id())


def get_publisher_client() -> pubsub_v1.PublisherClient:
    return pubsub_v1.PublisherClient()


def get_genai_client() -> genai.Client:
    """Gemini via Vertex AI. Never the public Gemini API — CLAUDE.md §5.

    gemini-3.5-flash only resolves at the "global" Vertex AI endpoint in
    this project — it 404s at regional locations like us-central1. Do not
    revert this to a region.
    """
    location = os.environ.get("VERTEX_AI_LOCATION", "global")
    return genai.Client(vertexai=True, project=get_project_id(), location=location)


def get_storage_client() -> storage.Client:
    return storage.Client(project=get_project_id())


def get_artifact_bucket_name() -> str:
    """The Cloud Storage bucket the Assessor stores imagery artifacts in.

    No bucket name is verified in CLAUDE.md §6 — it is not invented here.
    Must be set at deploy time; a missing value fails loudly rather than
    guessing (CLAUDE.md Rule 1). TODO(verify): record the real bucket name
    in CLAUDE.md §6 once provisioned.
    """
    bucket = os.environ.get("ARTIFACT_BUCKET")
    if not bucket:
        raise RuntimeError(
            "no artifact bucket configured: set ARTIFACT_BUCKET to the GCS "
            "bucket name for Assessor imagery artifacts"
        )
    return bucket
