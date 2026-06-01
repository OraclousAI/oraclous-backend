import hashlib
import json
from typing import Any

from pydantic import BaseModel


def _strip_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(item) for item in value]
    return value


def compute_content_hash(descriptor: dict | BaseModel) -> str:
    """Return SHA-256 hex digest of the canonical JSON form of a capability descriptor.

    Accepts a raw dict or a Pydantic BaseModel. None-valued keys are stripped so
    that model_dump() roundtrips produce the same hash as the equivalent raw dict
    (Optional fields appear as None in model_dump() but are absent from raw dicts).
    Keys are sorted at every nesting depth so insertion order is irrelevant.
    """
    if isinstance(descriptor, BaseModel):
        descriptor = descriptor.model_dump()
    canonical = json.dumps(_strip_none(descriptor), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
