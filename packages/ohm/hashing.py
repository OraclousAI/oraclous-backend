import hashlib
import json
from typing import Any, Union

from pydantic import BaseModel


def _strip_none(value: Any) -> Any:
    """Recursively remove None values from dicts so model_dump() roundtrips hash-stably."""
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(item) for item in value]
    return value


def compute_content_hash(descriptor: Union[dict, BaseModel]) -> str:
    """Return SHA-256 hex digest of the canonical JSON serialization of a descriptor.

    Accepts a raw dict or a Pydantic model instance. Keys are sorted at every
    depth so insertion order does not affect the output. None values are stripped
    so that model_dump() roundtrips produce the same hash as the original dict.
    """
    if isinstance(descriptor, BaseModel):
        descriptor_dict = descriptor.model_dump()
    else:
        descriptor_dict = descriptor
    canonical = json.dumps(_strip_none(descriptor_dict), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
