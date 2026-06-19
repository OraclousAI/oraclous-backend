"""OHM error taxonomy (ORAA-4 §21 domain layer; OHM v1.0 spec §7).

The full set is declared here so the slice-2 parser/loader extends it without churn; slice 1 raises
``OHMParseError`` / ``OHMSchemaError`` / ``OHMVersionError`` from the thin loader. Each maps to a
client error envelope in the route layer (a malformed harness is the caller's fault → HTTP 422/400).
"""

from __future__ import annotations


class OHMError(Exception):
    """Base for every OHM load/validation failure."""


class OHMParseError(OHMError):
    """The YAML is malformed."""


class OHMSchemaError(OHMError):
    """A required field is missing, or a field has the wrong type/value."""


class OHMReferenceError(OHMError):
    """A reference (capability/model/prompt/asset) cannot be resolved. (slice 2)"""


class OHMSignatureError(OHMError):
    """A signature is missing where required, or invalid. (slice 2)"""


class OHMVersionError(OHMError):
    """The ``ohm_version`` is outside the supported range."""


class OHMGovernanceError(OHMError):
    """The harness violates its policy-set constraints. (slice 3)"""


class OHMDagError(OHMError):
    """The team's member DAG is invalid — a cycle, a depends_on to an unknown member, or a
    duplicate member role. Raised by the v1.1 topological resolver; fail-closed."""
