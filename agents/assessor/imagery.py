"""NOAA NGS Emergency Response Imagery client, Hurricane Milton event.

Verified, not invented: NOAA's Milton imagery page
(https://storms.ngs.noaa.gov/storms/milton/index.html) sources its imagery
from Maxar Open Data at `s3://maxar-opendata/events/HurricaneMilton-Oct24/`,
which is also served as a public STAC (SpatioTemporal Asset Catalog) over
HTTPS at `MAXAR_MILTON_ROOT_COLLECTION_URL` below. This module walks that
real catalog for the acquisition closest to (but before) the FEMA-4834-DR-FL
incident start and the acquisition closest to (but at/after) it, within the
queried bounding box, and returns each item's own `properties.datetime` and
asset `href` — never a guessed date or URL (CLAUDE.md Rule 1). If no
suitable pair is found, it fails loudly rather than substituting a fallback.

This class is not unit tested directly — it performs real network I/O, like
the client constructors in `common/gcp.py`. `assessor.service` is tested
against `tests.fakes.FakeImageryClient`, which satisfies the same
`fetch_pair(bbox)` interface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from urllib.parse import urljoin

logger = logging.getLogger("prothesmia.assessor.imagery")

MAXAR_MILTON_ROOT_COLLECTION_URL = (
    "https://maxar-opendata.s3.amazonaws.com/events/HurricaneMilton-Oct24/"
    "collection.json"
)

# CLAUDE.md §6 anchor: Hurricane Milton incident period starts 2024-10-05.
MILTON_INCIDENT_START = date(2024, 10, 5)

# Bounds the STAC walk. This is a public, finite catalog (16 child
# acquisition collections for Milton) — not an open-ended crawl.
_MAX_DOCUMENTS = 200

ImageryLabel = Literal["pre_event", "post_event"]
BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class ImageryTile:
    label: ImageryLabel
    capture_date: date
    source_url: str
    content: bytes
    content_type: str


class ImageryUnavailableError(RuntimeError):
    """No pre-event/post-event tile pair could be found for the bbox."""


def _bbox_intersects(a: BBox, b: BBox) -> bool:
    a_min_lon, a_min_lat, a_max_lon, a_max_lat = a
    b_min_lon, b_min_lat, b_max_lon, b_max_lat = b
    return not (
        a_max_lon < b_min_lon
        or a_min_lon > b_max_lon
        or a_max_lat < b_min_lat
        or a_min_lat > b_max_lat
    )


class NOAAImageryClient:
    """Walks the Maxar Open Data STAC catalog for the Milton event.

    Constructed with an HTTP session (e.g. `requests.Session()`) so it can
    be swapped out in tests — mirrors `common.gcp`'s "clients passed as
    arguments" pattern.
    """

    def __init__(
        self,
        session,
        root_collection_url: str = MAXAR_MILTON_ROOT_COLLECTION_URL,
        incident_start: date = MILTON_INCIDENT_START,
    ):
        self._session = session
        self._root_collection_url = root_collection_url
        self._incident_start = incident_start

    def fetch_pair(self, bbox: BBox) -> tuple[ImageryTile, ImageryTile]:
        pre_candidates: list[tuple[date, str]] = []
        post_candidates: list[tuple[date, str]] = []

        for item_datetime, href in self._walk_items(
            self._root_collection_url, bbox, visited=set(), budget=[_MAX_DOCUMENTS]
        ):
            captured = item_datetime.date()
            if captured < self._incident_start:
                pre_candidates.append((captured, href))
            else:
                post_candidates.append((captured, href))

        if not pre_candidates or not post_candidates:
            raise ImageryUnavailableError(
                f"no pre/post-event imagery pair found intersecting bbox "
                f"{bbox}: {len(pre_candidates)} pre-event, "
                f"{len(post_candidates)} post-event candidates"
            )

        pre_date, pre_href = max(pre_candidates, key=lambda pair: pair[0])
        post_date, post_href = min(post_candidates, key=lambda pair: pair[0])

        pre_tile = self._download(
            label="pre_event", capture_date=pre_date, href=pre_href
        )
        post_tile = self._download(
            label="post_event", capture_date=post_date, href=post_href
        )
        return pre_tile, post_tile

    def _download(self, *, label: ImageryLabel, capture_date: date, href: str) -> ImageryTile:
        response = self._session.get(href, timeout=30)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        return ImageryTile(
            label=label,
            capture_date=capture_date,
            source_url=href,
            content=response.content,
            content_type=content_type,
        )

    def _walk_items(self, url: str, bbox: BBox, visited: set[str], budget: list[int]):
        if url in visited or budget[0] <= 0:
            return
        visited.add(url)
        budget[0] -= 1

        response = self._session.get(url, timeout=30)
        response.raise_for_status()
        doc = response.json()

        if doc.get("type") == "Feature":
            yield from self._item_as_candidate(doc, bbox)
            return

        extent_bbox = doc.get("extent", {}).get("spatial", {}).get("bbox")
        if extent_bbox and not any(_bbox_intersects(tuple(b), bbox) for b in extent_bbox):
            return

        for link in doc.get("links", []):
            if link.get("rel") not in ("child", "item"):
                continue
            child_url = self._resolve_href(url, link.get("href", ""))
            if child_url:
                yield from self._walk_items(child_url, bbox, visited, budget)

    def _item_as_candidate(self, item: dict, bbox: BBox):
        item_bbox = item.get("bbox")
        if item_bbox and not _bbox_intersects(tuple(item_bbox), bbox):
            return
        props = item.get("properties", {})
        dt_raw = props.get("datetime")
        if not dt_raw:
            logger.error("assessor.imagery.item_missing_datetime")
            return
        item_datetime = datetime.fromisoformat(dt_raw.replace("Z", "+00:00"))
        asset_href = self._pick_asset_href(item.get("assets", {}))
        if asset_href:
            yield item_datetime, asset_href

    @staticmethod
    def _pick_asset_href(assets: dict) -> str | None:
        for key in ("visual", "browse", "thumbnail"):
            asset = assets.get(key)
            if asset and asset.get("href"):
                return asset["href"]
        for asset in assets.values():
            if str(asset.get("type", "")).startswith("image/") and asset.get("href"):
                return asset["href"]
        return None

    @staticmethod
    def _resolve_href(base_url: str, href: str) -> str | None:
        if not href:
            return None
        if href.startswith("http://") or href.startswith("https://"):
            return href
        return urljoin(base_url, href)
