"""GCP client construction.

Kept separate from business logic so the Clock's decision-making
(`clock/service.py`, `statutes/rules.py`) can be unit tested without a GCP
credential or network access — only this module talks to real clients.
"""

from __future__ import annotations

import os

import google.auth
from google.cloud import firestore, pubsub_v1


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
