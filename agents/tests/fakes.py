"""Minimal in-memory fakes for Firestore, Pub/Sub, Cloud Storage, the NOAA
imagery client, and the Gemini client used by the Clock, Escalator, and
Assessor tests.

These implement only the surface the three agents' service modules touch:
`collection().where().stream()`, `collection().document(id).create()`,
`.get()`, `.update()`, `publisher.publish()` / `publisher.topic_path()`,
`storage_client.bucket().blob().upload_from_string()`,
`imagery_client.fetch_pair()`, and `genai_client.models.generate_content()`.
No rasterio/GDAL read happens here either — `FakeImageryClient` returns
prebuilt chips.
No network, no GCP or Vertex AI credential required.
"""

from __future__ import annotations

from types import SimpleNamespace

from google.api_core.exceptions import AlreadyExists, NotFound


class FakeDocSnapshot:
    def __init__(self, doc_id: str, data: dict, exists: bool = True):
        self.id = doc_id
        self._data = data
        self.exists = exists

    def to_dict(self) -> dict | None:
        return dict(self._data) if self.exists else None


class FakeDocumentRef:
    def __init__(self, store: dict, doc_id: str):
        self._store = store
        self._doc_id = doc_id

    def create(self, data: dict) -> None:
        if self._doc_id in self._store:
            raise AlreadyExists(f"document already exists: {self._doc_id}")
        self._store[self._doc_id] = dict(data)

    def get(self) -> FakeDocSnapshot:
        if self._doc_id in self._store:
            return FakeDocSnapshot(self._doc_id, self._store[self._doc_id])
        return FakeDocSnapshot(self._doc_id, {}, exists=False)

    def update(self, data: dict) -> None:
        if self._doc_id not in self._store:
            raise NotFound(f"document not found: {self._doc_id}")
        self._store[self._doc_id].update(data)


class FakeQuery:
    def __init__(self, docs: dict, field: str, value: object):
        self._docs = docs
        self._field = field
        self._value = value

    def stream(self):
        for doc_id, data in self._docs.items():
            if data.get(self._field) == self._value:
                yield FakeDocSnapshot(doc_id, data)


class FakeCollection:
    def __init__(self, store: dict):
        self._store = store

    def document(self, doc_id: str) -> FakeDocumentRef:
        return FakeDocumentRef(self._store, doc_id)

    def where(self, filter) -> FakeQuery:  # noqa: A002 - matches real API shape
        field, op, value = filter.field_path, filter.op_string, filter.value
        assert op == "==", f"fake only supports equality filters, got {op}"
        return FakeQuery(self._store, field, value)


class FakeFirestoreClient:
    def __init__(self):
        self.collections: dict[str, dict] = {}

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self.collections.setdefault(name, {}))


class FakePublisherClient:
    def __init__(self):
        self.published: list[tuple[str, bytes, dict]] = []

    def topic_path(self, project: str, topic: str) -> str:
        return f"projects/{project}/topics/{topic}"

    def publish(self, topic_path: str, data: bytes, **attrs) -> None:
        self.published.append((topic_path, data, attrs))


class FakeGenAIModels:
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.calls: list[dict] = []

    def generate_content(self, *, model: str, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return SimpleNamespace(text=self.response_text)


class FakeGenAIClient:
    """Stands in for `google.genai.Client`. `response_text` is the canned
    drafting output the test controls — the fake never actually formats
    prose, so tests assert on what was sent in (the prompt) and on how the
    service reacts to the canned response coming back out."""

    def __init__(self, response_text: str = ""):
        self.models = FakeGenAIModels(response_text)


class FakeBlob:
    def __init__(self, bucket: "FakeBucket", name: str):
        self._bucket = bucket
        self.name = name
        self.content_type: str | None = None

    def upload_from_string(self, data: bytes, content_type: str | None = None) -> None:
        self.content_type = content_type
        self._bucket.blobs[self.name] = data


class FakeBucket:
    def __init__(self, name: str):
        self.name = name
        self.blobs: dict[str, bytes] = {}

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)


class FakeStorageClient:
    """Stands in for `google.cloud.storage.Client`."""

    def __init__(self):
        self.buckets: dict[str, FakeBucket] = {}

    def bucket(self, name: str) -> FakeBucket:
        return self.buckets.setdefault(name, FakeBucket(name))


class FakeImageryClient:
    """Stands in for `assessor.imagery.NOAAImageryClient`. Tests configure
    `pre_chip`/`post_chip` (both `assessor.imagery.ImageryChip`) or `error`
    to raise, and can assert on `bbox_calls` to check what bbox the service
    asked for.

    The real client window-reads a remote COG through GDAL; this fake does
    no I/O at all, so tests hand it small in-memory chip bytes and still
    exercise the service's validation, provenance, and storage logic."""

    def __init__(self, pre_chip=None, post_chip=None, error: Exception | None = None):
        self.pre_chip = pre_chip
        self.post_chip = post_chip
        self.error = error
        self.bbox_calls: list[tuple] = []

    def fetch_pair(self, bbox):
        self.bbox_calls.append(bbox)
        if self.error is not None:
            raise self.error
        return self.pre_chip, self.post_chip
