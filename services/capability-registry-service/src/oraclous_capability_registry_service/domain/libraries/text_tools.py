"""Curated ``text-tools`` library (#488) — pure, deterministic, stdlib-only text functions.

Each function takes validated kwargs and returns a ``dict`` (the execution's ``output_data`` shape).
No I/O, no network, no external deps — so the tool group is keyless and the e2e is self-contained.
Mounted as a tool group by :mod:`registry`; dispatched in-process by :class:`LibraryGroupExecutor`.
"""

from __future__ import annotations

import re

# Bounded + non-overlapping so the match is LINEAR (no catastrophic backtracking): the dot lives
# only in a repeated label group, never in a label class, every quantifier length-capped. Prior bad:
# `[A-Za-z0-9.-]+\.[A-Za-z]{2,}` overlapped dot+letters and backtracked quadratically (#488 fix).
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,8}")


def word_count(text: str) -> dict:
    """Count whitespace-delimited words in ``text``."""
    return {"count": len(text.split())}


def to_upper(text: str) -> dict:
    """Upper-case ``text``."""
    return {"result": text.upper()}


def extract_emails(text: str) -> dict:
    """Extract the distinct e-mail addresses in ``text`` (sorted, deterministic)."""
    return {"emails": sorted({m.group(0) for m in _EMAIL_RE.finditer(text)})}
