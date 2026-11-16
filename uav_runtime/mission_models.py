"""Shared identifiers, geometry validation errors and stable JSON digests."""
import hashlib
import json
import re


class MissionError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def require(condition, code, message, **details):
    if not condition:
        raise MissionError(code, message, **details)


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value),
            "INVALID_INPUT", "invalid identifier")
    return value


def digest(value):
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                             allow_nan=False, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise MissionError("INVALID_INPUT", "finite JSON values required") from exc
    return hashlib.sha256(encoded).hexdigest()
