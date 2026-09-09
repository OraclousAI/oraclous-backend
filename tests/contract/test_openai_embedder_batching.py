"""Contract: `OpenAIEmbedder.embed()` batches, and never trusts the provider's response order.

Coverage gap found at review: the batching-by-256 and the re-sort of the response by `.index` had
no test anywhere in the repo, on either side. That mattered less while the class was one service's
private detail; it is now shared infrastructure in `packages/embedding` that both the write side
(embedding every chunk of a document) and the read side (embedding one query) depend on, and both
of those behaviours are silent when they break.

Silent is the point. A dropped or mis-ordered batch does not raise — it stores a chunk's vector
against another chunk's text, and every later cosine is computed on that. Nothing downstream can
detect it, because a wrong vector still scores plausibly, which is the whole failure class #643
exists to close. So the fake client here deliberately answers OUT OF ORDER: an implementation that
zips the response straight back onto its inputs passes on real providers most of the time and fails
here every time.

No network: `embed()` constructs its client at call time via `from openai import OpenAI`, so the
attribute on the module is what gets replaced.
"""

from __future__ import annotations

import pytest
from oraclous_embedding import OpenAIEmbedder

pytestmark = pytest.mark.unit

_BATCH = 256  # the batch ceiling the implementation splits on


class _Item:
    """One element of an embeddings response: its position in the REQUEST, and its vector."""

    def __init__(self, index: int, embedding: list[float]) -> None:
        self.index = index
        self.embedding = embedding


class _Response:
    def __init__(self, data: list[_Item]) -> None:
        self.data = data


class _Embeddings:
    def __init__(self, recorder: list[dict]) -> None:
        self._recorder = recorder

    def create(self, *, model: str, input: list[str], dimensions: int) -> _Response:  # noqa: A002
        self._recorder.append({"model": model, "input": list(input), "dimensions": dimensions})
        # Answer with each input's ORDINAL as its vector, so a caller can prove which text produced
        # which vector — then hand the items back REVERSED, which a provider is entitled to do.
        items = [_Item(i, [float(i)] * dimensions) for i in range(len(input))]
        return _Response(list(reversed(items)))


class _FakeClient:
    def __init__(self, recorder: list[dict]) -> None:
        self.embeddings = _Embeddings(recorder)


@pytest.fixture(autouse=True)
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Replace the openai client for EVERY test here; return the requests the embedder made.

    Autouse on purpose: a test in this file that forgot the fixture would build a real client and
    reach the network, which is exactly the kind of accident a shared-package test must not make.
    """
    import openai

    recorded: list[dict] = []
    monkeypatch.setattr(openai, "OpenAI", lambda **_kw: _FakeClient(recorded))
    return recorded


def _embedder(dim: int = 4) -> OpenAIEmbedder:
    return OpenAIEmbedder(api_key="sk-test", model="text-embedding-3-small", dim=dim)


# ── batching ─────────────────────────────────────────────────────────────────────────────────


def test_a_small_input_is_one_request(calls: list[dict]) -> None:
    vectors = _embedder().embed(["a", "b", "c"])

    assert len(calls) == 1
    assert calls[0]["input"] == ["a", "b", "c"]
    assert len(vectors) == 3


def test_an_input_over_the_batch_ceiling_is_split(calls: list[dict]) -> None:
    """A real document runs to thousands of chunks; sending them as one request fails outright at
    the provider, so the split is load-bearing rather than an optimisation."""
    texts = [f"t{i}" for i in range(_BATCH + 44)]

    vectors = _embedder().embed(texts)

    assert [len(c["input"]) for c in calls] == [_BATCH, 44]
    assert len(vectors) == len(texts)


def test_every_input_appears_exactly_once_across_the_batches(calls: list[dict]) -> None:
    """The split must partition the input — a boundary that overlaps or skips would embed a chunk
    twice, or leave one silently unembedded."""
    texts = [f"t{i}" for i in range(_BATCH * 2 + 7)]

    _embedder().embed(texts)

    sent = [t for call in calls for t in call["input"]]
    assert sent == texts


def test_an_empty_input_makes_no_request(calls: list[dict]) -> None:
    assert _embedder().embed([]) == []
    assert calls == []


# ── the response order is never trusted ──────────────────────────────────────────────────────


def test_vectors_come_back_in_the_INPUT_order_not_the_response_order() -> None:
    """The fake answers reversed. Each vector is filled with its input ordinal, so the assertion
    fails loudly if the response is zipped back onto the inputs as received."""
    embedder = _embedder(dim=2)
    vectors = embedder.embed(["first", "second", "third"])

    assert vectors == [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0]]


def test_the_order_is_restored_within_every_batch_not_just_the_first() -> None:
    """`.index` is per-REQUEST, so it restarts at 0 in the second batch. A sort applied across the
    accumulated result rather than per batch would interleave the two."""
    embedder = _embedder(dim=1)
    texts = [f"t{i}" for i in range(_BATCH + 3)]

    vectors = embedder.embed(texts)

    assert len(vectors) == len(texts)
    assert vectors[:3] == [[0.0], [1.0], [2.0]]  # first batch, in input order
    assert vectors[_BATCH : _BATCH + 3] == [[0.0], [1.0], [2.0]]  # second batch, its own ordinals


# ── what the request carries ─────────────────────────────────────────────────────────────────


def test_the_configured_model_and_dimension_reach_the_provider(calls: list[dict]) -> None:
    """`dimensions` is what makes the stored vector match `embedding_dim`, and the model is half of
    the identity string a chunk is stamped with — a wrong one here is a whole workspace embedded
    into a space that does not match its own recorded identity."""
    OpenAIEmbedder(api_key="sk-test", model="text-embedding-3-large", dim=1536).embed(["x"])

    assert calls[0]["model"] == "text-embedding-3-large"
    assert calls[0]["dimensions"] == 1536
