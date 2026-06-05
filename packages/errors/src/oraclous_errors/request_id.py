"""Server-authoritative request-correlation id minting for the ORA-37 envelope."""

from __future__ import annotations

import re
import secrets

#: The contract's requestId shape: a literal ``req_`` prefix + alphanumerics only.
REQUEST_ID_PATTERN = re.compile(r"^req_[0-9A-Za-z]+$")


def new_request_id() -> str:
    """Mint an opaque, unguessable correlation id matching ``^req_[0-9A-Za-z]+$``.

    ``secrets.token_hex`` yields lowercase hex only, so the value can never match a
    forbidden-substrings pattern (no IPs, emails, or tokens) and is safe as the
    only trace handle exposed in an error body. The id is minted server-side; a
    client-supplied correlation header is never trusted as the requestId.
    """
    return "req_" + secrets.token_hex(16)
