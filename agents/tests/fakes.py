"""Minimal in-memory fakes for Firestore and Pub/Sub used by clock tests.

These implement only the surface `clock.service.run_clock_tick` touches:
`collection().where().stream()`, `collection().document(id).create()`, and
`publisher.publish()` / `publisher.topic_path()`. No network, no GCP
credential required.
"""

from __future__ import annotations

from google.api_core.exceptions import AlreadyExists


class FakeDocSnapshot:
    def __init__(self, doc_id: str, data: dict):
        self.id = doc_id
        self._data = data

    def to_dict(self) -> dict:
        return dict(self._data)


class FakeDocumentRef:
    def __init__(self, store: dict, doc_id: str):
        self._store = store
        self._doc_id = doc_id

    def create(self, data: dict) -> None:
        if self._doc_id in self._store:
            raise AlreadyExists(f"document already exists: {self._doc_id}")
        self._store[self._doc_id] = dict(data)


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
