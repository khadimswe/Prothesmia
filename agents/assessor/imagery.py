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

The `visual` asset on a real Maxar ARD item is a full-resolution
Cloud-Optimized GeoTIFF — 11 MB to 160 MB, confirmed by live HEAD requests.
Those are far too large to hand to a model inline, and downloading one whole
to look at a single parcel is waste. So this module never downloads the COG.
It opens it *remotely* through GDAL's `/vsicurl/` virtual filesystem, which
uses HTTP range requests against the COG's internal tiling and overviews,
reads only the pixel window covering the claim's bbox at a decimated
resolution, and encodes that parcel-sized chip to JPEG. Measured against the
verified `clm-001` pair, that is well under a tenth of the object fetched
over the wire, for a chip of a few hundred KB.

This class is not unit tested directly — it performs real network I/O, like
the client constructors in `common/gcp.py`. `assessor.service` is tested
against `tests.fakes.FakeImageryClient`, which satisfies the same
`fetch_pair(bbox)` interface.

The catalog shape this module assumes — a root `collection.json` with
`child` links to per-acquisition collections under
`ard/acquisition_collections/`, each with `item` links to per-tile STAC
items at `ard/{zoom}/{quadkey}/{date}/{acquisition_id}.json`, each with an
`assets` block carrying real `href`s — was confirmed by live requests
against the bucket, not assumed. `assessor.fixtures` records one verified
real pre/post-event item pair (`clm-001`'s) fetched this way, with its
real bbox, dates, and item/asset URLs.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from urllib.parse import urljoin

import rasterio
import rasterio.shutil
from rasterio.errors import WindowError
from rasterio.io import MemoryFile
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds

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

# The bbox CRS used throughout this repo: `claims.imagery_bbox` and the STAC
# catalog's own `bbox` fields are both lon/lat WGS84. The COGs are not — the
# verified `clm-001` pair is EPSG:32617 (UTM 17N) — so the bbox is always
# reprojected into the raster's own CRS before a window is computed.
BBOX_CRS = "EPSG:4326"

# GDAL settings for the `/vsicurl/` reads, applied per-call via
# `rasterio.Env` rather than by mutating os.environ. Measured honestly: on
# these plain-HTTPS S3 objects, cold-process reads transferred byte-for-byte
# the same amount with and without the first two settings (626,211 B either
# way for the verified `clm-001` pre-event window), because GDAL does not
# probe for siblings on this scheme. They are kept as cheap insurance
# against a source that would — not because they were measured to help here.
# GDAL_PAM_ENABLED=NO does earn its place: it stops GDAL writing `.aux.xml`
# sidecars while encoding.
VSICURL_GDAL_CONFIG = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF,.tiff",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "GDAL_PAM_ENABLED": "NO",
}

# Longest edge, in pixels, of the chip handed to the model. A parcel-sized
# window off a 0.3 m/px Maxar ARD tile is a few thousand pixels across; 1024
# keeps roof-level detail while landing the encoded chip in the hundreds of
# KB (measured: 255,405 B pre-event, 245,229 B post-event for the verified
# `clm-001` pair). CHIP_MAX_BYTES is a deliberately conservative refusal
# threshold well below any inline request limit — it is our own guard, not a
# figure quoted from the API. TODO(verify) if a published limit is ever cited.
CHIP_MAX_DIMENSION = 1024
CHIP_MAX_BYTES = 4 * 1024 * 1024

# JPEG, not PNG: the Maxar `visual` COG is itself JPEG-compressed internally,
# so a lossless re-encode buys no fidelity and costs ~5x the bytes (measured:
# 1.4 MB PNG vs 255 KB JPEG for the same verified `clm-001` pre-event window).
CHIP_CONTENT_TYPE = "image/jpeg"
_CHIP_JPEG_QUALITY = "85"
_CHIP_DRIVER = "JPEG"
_CHIP_SUFFIX = ".jpg"


@dataclass(frozen=True)
class ImageryChip:
    """A parcel-sized crop of a source COG, plus the provenance to retrace it.

    `content` is the encoded chip that goes to Cloud Storage and to the
    model. Everything else records exactly which pixels it came from:
    `source_url` is the COG, `source_window` is the (col_off, row_off,
    width, height) window in that COG's own pixel grid, `source_crs` is the
    CRS that window is expressed in, and `window_sha256` hashes the raw
    decoded pixel bytes read out of the COG *before* encoding — a value that
    can be recomputed from the source and the window alone.
    """

    label: ImageryLabel
    capture_date: date
    source_url: str
    source_crs: str
    requested_bbox: BBox
    chip_bounds: BBox
    source_window: tuple[int, int, int, int]
    decimation: int
    read_shape: tuple[int, int, int]
    read_bytes: int
    window_sha256: str
    content: bytes
    content_type: str
    sha256: str


class ImageryUnavailableError(RuntimeError):
    """No pre-event/post-event tile pair could be found for the bbox."""


class ChipReadError(RuntimeError):
    """A source COG could not be windowed into a usable chip."""


def read_chip(
    *,
    url: str,
    bbox: BBox,
    label: ImageryLabel,
    capture_date: date,
    max_dimension: int = CHIP_MAX_DIMENSION,
) -> ImageryChip:
    """Window-read `bbox` out of the remote COG at `url` and encode a chip.

    Opens the object over `/vsicurl/` — GDAL issues HTTP range requests
    against the COG's internal tiles and overviews, so only the bytes
    backing this window are transferred, never the whole file.
    """
    with rasterio.Env(**VSICURL_GDAL_CONFIG):
        with rasterio.open(f"/vsicurl/{url}") as dataset:
            if dataset.crs is None:
                raise ChipReadError(f"{label} source COG has no CRS: {url}")
            source_crs = str(dataset.crs)
            # The Maxar `visual` asset is 8-bit RGB (verified against the
            # `clm-001` pair). Anything else is a different product and must
            # not be silently re-encoded as if it were 8-bit.
            if set(dataset.dtypes) != {"uint8"}:
                raise ChipReadError(
                    f"{label} source COG is {dataset.dtypes}, not uint8: {url}"
                )

            bounds = transform_bounds(BBOX_CRS, dataset.crs, *bbox, densify_pts=21)
            full = Window(0, 0, dataset.width, dataset.height)
            requested = from_bounds(*bounds, transform=dataset.transform)
            try:
                window = requested.intersection(full)
            except WindowError as exc:
                raise ChipReadError(
                    f"{label} bbox {bbox} does not overlap source COG {url}"
                ) from exc
            window = window.round_offsets().round_lengths()
            if window.width < 1 or window.height < 1:
                raise ChipReadError(
                    f"{label} bbox {bbox} covers no whole pixel of source COG {url}"
                )

            # Bands: RGB where present, else the single band. An alpha band,
            # if the source carries one, is dropped — JPEG takes 1 or 3.
            indexes = tuple(range(1, 4)) if dataset.count >= 3 else (1,)

            decimation = max(
                1, math.ceil(max(window.width, window.height) / max_dimension)
            )
            out_height = max(1, round(window.height / decimation))
            out_width = max(1, round(window.width / decimation))

            pixels = dataset.read(
                indexes=indexes,
                window=window,
                out_shape=(len(indexes), out_height, out_width),
            )
            chip_bounds = transform_bounds(
                dataset.crs, BBOX_CRS, *dataset.window_bounds(window), densify_pts=21
            )
            # The decimated chip's own affine, in the source CRS.
            chip_transform = dataset.window_transform(window) * rasterio.Affine.scale(
                window.width / out_width, window.height / out_height
            )
            chip_crs = dataset.crs

    raw = pixels.tobytes()
    content = _encode_chip(pixels, crs=chip_crs, transform=chip_transform)
    if len(content) > CHIP_MAX_BYTES:
        raise ChipReadError(
            f"{label} chip encoded to {len(content)} bytes, over the "
            f"{CHIP_MAX_BYTES}-byte inline ceiling, from {url}"
        )

    logger.info(
        "assessor.imagery.chip_read",
        extra={
            "label": label,
            "decimation": decimation,
            "chip_bytes": len(content),
        },
    )
    return ImageryChip(
        label=label,
        capture_date=capture_date,
        source_url=url,
        source_crs=source_crs,
        requested_bbox=tuple(bbox),
        chip_bounds=tuple(chip_bounds),
        source_window=(
            int(window.col_off),
            int(window.row_off),
            int(window.width),
            int(window.height),
        ),
        decimation=decimation,
        read_shape=(len(indexes), out_height, out_width),
        read_bytes=len(raw),
        window_sha256=hashlib.sha256(raw).hexdigest(),
        content=content,
        content_type=CHIP_CONTENT_TYPE,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _encode_chip(pixels, *, crs, transform) -> bytes:
    """Encode a decimated uint8 array to `CHIP_CONTENT_TYPE`, in memory.

    GDAL's JPEG driver is CreateCopy-only, so the array is staged as an
    in-memory GTiff — georeferenced, so the staged raster is a true subset
    of the source — and copied through the JPEG driver. JPEG carries no
    geotransform of its own, so the chip's footprint travels separately in
    `ImageryChip.chip_bounds`, which is what reaches Firestore and the
    Cloud Storage manifest.
    """
    bands, height, width = pixels.shape
    profile = {
        "driver": "GTiff",
        "count": bands,
        "height": height,
        "width": width,
        "dtype": "uint8",
        "crs": crs,
        "transform": transform,
    }
    with rasterio.Env(**VSICURL_GDAL_CONFIG):
        with MemoryFile() as staged:
            with staged.open(**profile) as staged_dataset:
                staged_dataset.write(pixels)
            with MemoryFile(filename=f"chip{_CHIP_SUFFIX}") as encoded:
                rasterio.shutil.copy(
                    staged.name,
                    encoded.name,
                    driver=_CHIP_DRIVER,
                    QUALITY=_CHIP_JPEG_QUALITY,
                )
                return encoded.read()


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

    def fetch_pair(self, bbox: BBox) -> tuple[ImageryChip, ImageryChip]:
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

        pre_chip = read_chip(
            url=pre_href, bbox=bbox, label="pre_event", capture_date=pre_date
        )
        post_chip = read_chip(
            url=post_href, bbox=bbox, label="post_event", capture_date=post_date
        )
        return pre_chip, post_chip

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
