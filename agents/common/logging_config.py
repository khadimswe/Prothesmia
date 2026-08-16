"""Structured JSON logging.

CLAUDE.md §4: no PII in logs. Addresses, carrier names, claimant names, and
dollar figures are never logged anywhere in this service. `claim_id` is the
only structured identifier attached to a log line — it is an opaque key, not
personal data, and it is the correlation key across `clock_checks` and
`breaches`.

Callers must never pass free-form claim data (a Firestore document, a raw
dict from user input) into `extra=`. Pass `claim_id` and other scalar,
non-identifying fields only.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key != "message":
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
