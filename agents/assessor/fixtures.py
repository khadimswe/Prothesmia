"""Verified real Maxar imagery reference data for `clm-001`.

Every value below was confirmed live against the Maxar Open Data STAC
catalog for the `HurricaneMilton-Oct24` event (see `assessor.imagery` for
the client that walks this same catalog) — fetched with real HTTP requests
on 2026-08-20, not assumed (CLAUDE.md Rule 1). Nothing here is invented.

`parcel_id` is deliberately absent. No real parcel record has been
resolved for this location — Intake's parcel lookup is not built yet (see
`docs/Milestones`) — so seeding a parcel ID here would mean inventing one,
which CLAUDE.md Rule 1 forbids. Until Intake resolves a real parcel,
`assessor.service.run_assessment` correctly fails loudly for `clm-001`
with a missing-field error rather than guessing (see its `missing` check).

Location: a Maxar zoom-17 STAC tile (quadkey `031331031123`) near Dunedin,
Pinellas County, FL — within the FEMA-4834-DR-FL Hurricane Milton incident
area. Chosen because it is one of 35 quadkeys (out of the 16-collection
Milton catalog) with both a genuine pre-event and post-event acquisition,
confirmed by fetching the root collection, its 15 child acquisition
collections, both items' STAC JSON, and both `visual` asset URLs (HTTP
200, real `Content-Length`/`Content-Type`).
"""

from __future__ import annotations

# CLAUDE.md §6 anchor facts — not invented here, just referenced.
CLM_001_COUNTY = "Pinellas"
CLM_001_FEMA_DECLARATION = "FEMA-4834-DR-FL"

# (min_lon, min_lat, max_lon, max_lat) — confirmed to intersect both the
# pre-event and post-event item bboxes below, per
# `assessor.imagery._bbox_intersects`.
CLM_001_IMAGERY_BBOX: tuple[float, float, float, float] = (
    -82.745,
    28.015,
    -82.730,
    28.025,
)

_MAXAR_MILTON_ARD = (
    "https://maxar-opendata.s3.amazonaws.com/events/HurricaneMilton-Oct24/ard"
)

# Pre-event: captured 2024-06-03, before the 2024-10-05 incident start.
CLM_001_PRE_EVENT_ITEM = {
    "acquisition_collection_id": "1040010097D1F500",
    "quadkey": "031331031123",
    "datetime": "2024-06-03T16:25:10Z",
    "bbox": (
        -82.74930221355623,
        28.012710470176263,
        -82.72747431473579,
        28.02676840060579,
    ),
    "item_url": f"{_MAXAR_MILTON_ARD}/17/031331031123/2024-06-03/1040010097D1F500.json",
    "visual_asset_url": (
        f"{_MAXAR_MILTON_ARD}/17/031331031123/2024-06-03/1040010097D1F500-visual.tif"
    ),
}

# Post-event: captured 2024-10-10, within the incident period (ends
# 2024-11-02) and after incident start.
CLM_001_POST_EVENT_ITEM = {
    "acquisition_collection_id": "104001009C4AC400",
    "quadkey": "031331031123",
    "datetime": "2024-10-10T16:04:15Z",
    "bbox": (
        -82.77934628685418,
        28.01232911075098,
        -82.72747431473579,
        28.060921971390325,
    ),
    "item_url": f"{_MAXAR_MILTON_ARD}/17/031331031123/2024-10-10/104001009C4AC400.json",
    "visual_asset_url": (
        f"{_MAXAR_MILTON_ARD}/17/031331031123/2024-10-10/104001009C4AC400-visual.tif"
    ),
}
