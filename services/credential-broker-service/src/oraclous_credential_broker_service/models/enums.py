"""Credential enums (reshape of legacy ``app/models/enums.py``)."""

from __future__ import annotations

import enum


class CredentialType(enum.StrEnum):
    OAUTH = "oauth"
    API_KEY = "api_key"
    RAW = "raw"
