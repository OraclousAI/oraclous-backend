"""The deterministic ``citation_id`` — the whole security mechanism (§CITE rev3).

```
citation_id = "cit_" + sha256(source_system ‖ 0x00 ‖ source_id ‖ 0x00 ‖ revision)[:32]
```

Three fields, two NUL bytes, 32 hex characters. Determinism buys three things at once: re-reading
the same revision yields the same id (an idempotent refresh); a new revision yields a different id,
which makes supersession computable without a supersession table; and an id a model invents is not
in the set the platform served, so it fails the answer-time gate.

Nothing else enters the identity. The ``url`` is out, so a repository rename never invalidates a
stored citation. Sub-document precision is out (rev3 deleted the locator), so re-chunking a
document never changes its ``citation_id`` — a citation already stored in a published answer keeps
resolving.
"""

from __future__ import annotations

import hashlib

_PREFIX = "cit_"
_DIGEST_CHARS = 32
_SEPARATOR = b"\x00"


def compute_citation_id(source_system: str, source_id: str, revision: str) -> str:
    """The §CITE identity of one document version.

    The separator is a NUL BYTE, not a literal string. A fixture minted under one convention would
    stop resolving against the other, so the byte is spelled out here rather than inlined.
    """
    payload = _SEPARATOR.join(
        part.encode("utf-8") for part in (source_system, source_id, revision)
    )
    return _PREFIX + hashlib.sha256(payload).hexdigest()[:_DIGEST_CHARS]
