"""The intake read-back's pure rules (domain layer) — no I/O, no model (#866).

Two things must be decidable without calling anything.

**The length floor.** "Too vague" is a deterministic character count, checked BEFORE the model is
called at all, never the model's own judgement (ruled on #866). A model-judged refusal is not
reproducible: the same idea passes on Monday and is refused on Tuesday, and the founder has no way
to understand why. Above the floor the endpoint always restates, marking whatever it could not read
as ``inferred`` — there is no second, softer refusal hiding behind the model.

**The answer's shape.** A model returns whatever it likes; the endpoint's contract is narrow, and
this module holds it rather than trusting the model to. The restatement is an ordered array of
spans, never one blob of prose: the screen's whole job is to show the inferred spans *as* inferred
so the founder can correct them, and it cannot do that if it cannot tell them apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

#: Below this many characters of actual idea, the endpoint refuses without calling a model.
#: Pinned as a number because the screen tells the founder how much more to write.
IDEA_MIN_CHARS: Final[int] = 80

#: Capped by the ENDPOINT, not by the caller: a chatty model must not put a fourth question on
#: the screen. Questions asked before the system knows anything are generic, and three focused
#: ones are the design's whole point.
MAX_QUESTIONS: Final[int] = 3

_SOURCES: Final[frozenset[str]] = frozenset({"read", "inferred"})
_KINDS: Final[frozenset[str]] = frozenset({"text", "choice"})


class ReadbackShapeError(ValueError):
    """The model's answer does not fit the endpoint's contract.

    Always a curated 4xx at the service seam, never a 500: a model answering in prose or inventing
    a third ``source`` is an expected outcome of asking a model, not a server fault.
    """


@dataclass(frozen=True)
class Span:
    """One piece of the restatement. ``source`` is exactly ``read`` or ``inferred``: ``read``
    means the span is grounded in the founder's own words, ``inferred`` means the system supplied
    it. The screen renders the two differently, so a third value has no rendering at all."""

    text: str
    source: str


@dataclass(frozen=True)
class Question:
    """One question derived from the idea. ``choice`` carries a non-empty ``options`` (the screen
    renders them as buttons); ``text`` carries an empty one."""

    id: str
    text: str
    kind: str
    options: list[str]


def idea_meets_floor(idea: str) -> bool:
    """Whether the idea is long enough to read back. Surrounding whitespace does not count —
    otherwise padding with spaces buys a confident restatement of nothing."""
    return len(idea.strip()) >= IDEA_MIN_CHARS if isinstance(idea, str) else False


def _parse_spans(raw: Any) -> list[Span]:
    if not isinstance(raw, list) or not raw:
        # Above the floor the endpoint always restates. No spans means the model declined, and a
        # silently empty paragraph on the screen is worse than a refusal the founder can act on.
        raise ReadbackShapeError("restatement must be a non-empty ordered array of spans")
    spans: list[Span] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ReadbackShapeError("each restatement entry must be a span object")
        text = item.get("text")
        source = item.get("source")
        if not isinstance(text, str) or not text.strip():
            raise ReadbackShapeError("a span carries non-empty text")
        if source not in _SOURCES:
            raise ReadbackShapeError("a span's source is exactly 'read' or 'inferred'")
        spans.append(Span(text=text, source=source))
    return spans


def _parse_questions(raw: Any) -> list[Question]:
    if not isinstance(raw, list):
        raise ReadbackShapeError("questions must be an array (an empty one is fine)")
    questions: list[Question] = []
    seen: set[str] = set()
    for item in raw[:MAX_QUESTIONS]:  # the cap is the endpoint's, applied before anything else
        if not isinstance(item, dict):
            raise ReadbackShapeError("each question must be an object")
        qid = item.get("id")
        text = item.get("text")
        kind = item.get("kind")
        options = item.get("options", [])
        if not isinstance(qid, str) or not qid.strip():
            raise ReadbackShapeError("a question carries a non-empty id")
        if qid in seen:
            # The answers come back keyed by id (``inputs.answers``, #849). Two questions sharing
            # an id means one of the founder's answers silently overwrites the other.
            raise ReadbackShapeError("question ids are unique")
        if not isinstance(text, str) or not text.strip():
            raise ReadbackShapeError("a question carries non-empty text")
        if kind not in _KINDS:
            raise ReadbackShapeError("a question's kind is 'text' or 'choice'")
        if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
            raise ReadbackShapeError("a question's options are a list of strings")
        if kind == "choice" and not options:
            # The screen renders a choice as buttons. No options means no buttons, and a question
            # the founder can look at but not answer.
            raise ReadbackShapeError("a 'choice' question carries at least one option")
        if kind == "text" and options:
            raise ReadbackShapeError("a 'text' question carries no options")
        seen.add(qid)
        questions.append(Question(id=qid, text=text, kind=kind, options=list(options)))
    return questions


def parse_readback(payload: Any) -> tuple[list[Span], list[Question]]:
    """Validate a model's answer into ordered spans + at most three questions.

    Raises ``ReadbackShapeError`` for anything the screen could not render honestly.
    """
    if not isinstance(payload, dict):
        raise ReadbackShapeError("the read-back answer must be a JSON object")
    if "questions" not in payload:
        raise ReadbackShapeError("the read-back answer names its questions (an empty list is fine)")
    return _parse_spans(payload.get("restatement")), _parse_questions(payload["questions"])
