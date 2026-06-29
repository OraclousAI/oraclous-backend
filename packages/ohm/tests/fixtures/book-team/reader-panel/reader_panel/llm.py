"""Anthropic client wrapper for the panel.

Design notes:
- Structured output via `output_config.format` (json_schema) + json.loads — no
  pydantic in our own code.
- Adaptive thinking (`thinking={"type":"adaptive"}`) on the fidelity-critical
  calls; effort via `output_config.effort`.
- Prompt caching: the chapter text is identical across every persona reacting to
  one scope, so it lives in a cache_control'd shared system block; the small
  per-persona block goes after it. The first reaction primes the cache; the rest
  read it at ~0.1x cost.
- Web gather uses the server-side web_search tool with a pause_turn loop.
- `mock=True` short-circuits every method to deterministic offline data.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from . import config, mock

# --- JSON schemas for structured output (no min/maxItems; enforce in prompt) --

_SUBREACTION_PROPS = {
    "probability": {"type": "number"},
    "gut_reaction": {"type": "string"},
    "pulled_in_at": {"type": "string"},
    "put_down_at": {"type": "string"},
    "delighted_by": {"type": "string"},
    "annoyed_by": {"type": "string"},
    "strongest_objection": {"type": "string"},
    "star_rating": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
    "one_paragraph_review": {"type": "string"},
    "would_recommend_to": {"type": "string"},
    "emotion_felt": {"type": "string"},
}
_SUBREACTION = {
    "type": "object",
    "additionalProperties": False,
    "properties": _SUBREACTION_PROPS,
    "required": list(_SUBREACTION_PROPS),
}

REACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"reactions": {"type": "array", "items": _SUBREACTION}},
    "required": ["reactions"],
}

NETWORK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "updated": {"type": "array", "items": _SUBREACTION},
        "did_shift": {"type": "boolean"},
        "shifted_because": {"type": "string"},
        "moved_toward": {"type": "string", "enum": ["more_positive", "more_negative", "none"]},
    },
    "required": ["updated", "did_shift", "shifted_because", "moved_toward"],
}

_ARCHETYPE_PROPS = {
    "persona_id": {"type": "string"},
    "archetype_name": {"type": "string"},
    "panel_weight": {"type": "number"},
    "provenance": {"type": "string"},
    "backstory": {"type": "string"},
    "reading_habits": {"type": "string"},
    "reading_level": {"type": "string"},
    "jtbd": {"type": "string"},
    "prior_beliefs": {"type": "string"},
    "value_prior": {"type": "string"},
    "resonance": {"type": "string"},
    "complaint": {"type": "string"},
    "annoyances": {"type": "array", "items": {"type": "string"}},
    "voice_sample": {"type": "string"},
    "devils_advocate": {"type": "boolean"},
}
MINE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "archetypes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": _ARCHETYPE_PROPS,
                "required": list(_ARCHETYPE_PROPS),
            },
        }
    },
    "required": ["archetypes"],
}

SYNTH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ranked_objections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "objection": {"type": "string"},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "frequency_signal": {"type": "string"},
                    "segments_affected": {"type": "array", "items": {"type": "string"}},
                    "tag": {"type": "string", "enum": ["confusing", "challenging"]},
                    "fix_or_protect": {"type": "string", "enum": ["fix", "protect"]},
                    "representative_quote": {"type": "string"},
                },
                "required": [
                    "objection", "severity", "frequency_signal", "segments_affected",
                    "tag", "fix_or_protect", "representative_quote",
                ],
            },
        },
        "what_resonated": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["ranked_objections", "what_resonated", "summary"],
}

# Universal anti-sycophancy / anti-mode-collapse framing prepended to reactions.
_BIAS_GUARDRAILS = (
    "You are a real reader, not an assistant. Do not be agreeable to please. React "
    "honestly, including disliking things. Never break character or mention being an AI. "
    "You MUST name your strongest objection even if you mostly liked it. When you give a "
    "star rating, judge on the content, not on any option ordering."
)


class Client:
    def __init__(self, cfg: config.RunConfig):
        self.cfg = cfg
        self.mock = cfg.mock
        self._client = None
        if not self.mock:
            if not cfg.resolve_credential():
                sys.stderr.write(
                    "[reader-panel] no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN in env; "
                    "relying on an `ant auth login` profile if present.\n"
                )
            import anthropic  # imported lazily so --mock needs no install
            self._client = anthropic.Anthropic()

    # --- low-level helpers ---------------------------------------------------

    def _structured(self, model, system, user, schema, *, max_tokens=16000,
                    thinking=True, effort="high") -> dict[str, Any]:
        # 16k keeps non-streaming calls under the SDK timeout guard while leaving
        # room for adaptive-thinking tokens (which count toward max_tokens) so the
        # structured JSON isn't truncated.
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": schema}
        }
        params: dict[str, Any] = dict(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
        )
        if thinking:
            params["thinking"] = {"type": "adaptive"}
            output_config["effort"] = effort
        resp = self._client.messages.create(**params)
        text = next((b.text for b in resp.content if b.type == "text"), "")
        # A refusal yields no text; max_tokens truncation (adaptive-thinking tokens
        # share the budget) yields partial JSON. Degrade to {} so one call can't
        # abort the whole fan-out; callers use .get() defaults.
        if not text:
            sys.stderr.write(f"[reader-panel] empty response (model={model}, "
                             f"stop_reason={resp.stop_reason}); skipping this call\n")
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            sys.stderr.write(f"[reader-panel] truncated/unparseable JSON (model={model}, "
                             f"stop_reason={resp.stop_reason}); skipping this call\n")
            return {}

    # --- corpus --------------------------------------------------------------

    def gather_themes(self, title: str, author: str) -> tuple[str, list[str]]:
        if self.mock:
            return mock.themes(title, author)
        prompt = (
            f"Use web search to find what REAL readers say about the book "
            f'"{title}" by {author}, across Amazon, Goodreads, and Reddit. '
            "Summarize a Resonance / Complaint / Emotion themes matrix:\n"
            "- Resonance: ideas readers praise (paraphrase the language they use).\n"
            "- Complaint: recurring gripes, grouped by pattern.\n"
            "- Emotion: the affective state readers report after finishing.\n"
            "- Reader archetypes you can observe (who reads this, what they want, "
            "what splits the reviews).\n\n"
            "IMPORTANT: paraphrase themes and representative reactions — do NOT reproduce "
            "verbatim copyrighted review text, names, or personal details. Output markdown."
        )
        tools = [{"type": "web_search_20260209", "name": "web_search"}]
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        resp = None
        for _ in range(6):  # bound the server-side tool loop
            resp = self._client.messages.create(
                model=config.GATHER_MODEL, max_tokens=8000,
                thinking={"type": "adaptive"}, tools=tools, messages=messages,
            )
            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue
            break
        if resp is None or resp.stop_reason == "pause_turn":
            # exhausted the loop still mid-search: the final themes summary isn't
            # present yet — fail loudly instead of writing an empty/partial corpus.
            raise RuntimeError(
                f"web_search did not converge for {title!r} by {author!r} in 6 turns; "
                f"re-run `reader-panel corpus` to retry."
            )
        text = "".join(getattr(b, "text", "") for b in resp.content if b.type == "text")
        return text, _extract_citations(resp)

    # --- personas ------------------------------------------------------------

    def mine_archetypes(self, corpus_text: str, n: int, existing_hint: str = "") -> list[dict]:
        if self.mock:
            return mock.archetypes(n)
        system = [{
            "type": "text",
            "text": (
                "You segment a corpus of reader-review themes into data-grounded reader "
                "archetypes for a synthetic reader panel. Each archetype is a distinct "
                "first-person reader with a SIGNATURE complaint (distinct per archetype), a "
                "job-to-be-done, a reading level, and a value prior. Ground every field in "
                "the corpus; do not invent demographics the corpus doesn't support. Preserve "
                "WITHIN-segment variance — these are real readers, not caricatures. Set "
                "panel_weight to each archetype's share of the readership (weights sum to ~1.0). "
                "Mark devils_advocate=true for the critic/contrarian archetypes."
            ),
            "cache_control": {"type": "ephemeral"},
        }]
        user = (
            f"Produce exactly {n} archetypes (allocate copies across recurring patterns so "
            f"weights stay realistic). {existing_hint}\n\n=== REVIEW-THEME CORPUS ===\n{corpus_text}"
        )
        out = self._structured(config.MINE_MODEL, system, user, MINE_SCHEMA, effort="high")
        return out.get("archetypes", [])

    # --- reactions -----------------------------------------------------------

    def react(self, shared_system: list[dict], persona, samples: int) -> dict[str, Any]:
        if self.mock:
            return mock.react(persona.persona_id, samples)
        persona_block = {
            "type": "text",
            "text": (
                f"{_BIAS_GUARDRAILS}\n\n{persona.as_prompt_block()}\n\n"
                f"Using VERBALIZED SAMPLING, return {samples} distinct reactions you might "
                f"plausibly have, each with a probability (the probabilities sum to ~1.0). "
                f"For each: gut_reaction; the exact line that pulled you in (pulled_in_at) and "
                f"where you put it down (put_down_at); what delighted/annoyed you (tie one to "
                f"YOUR signature complaint); your strongest_objection (required); a 1-5 "
                f"star_rating and a one_paragraph_review in your voice; would_recommend_to; and "
                f"emotion_felt (use one of these tokens if it fits: {', '.join(config.TARGET_STATES)})."
            ),
        }
        return self._structured(
            config.REACT_MODEL, shared_system + [persona_block],
            "Read the chapter above as this reader and react now.",
            REACT_SCHEMA, effort="high",
        )

    def network_react(self, shared_system: list[dict], persona, prior: dict[str, Any]) -> dict[str, Any]:
        if self.mock:
            return mock.network_react(persona.persona_id, prior)
        persona_block = {
            "type": "text",
            "text": (
                f"{_BIAS_GUARDRAILS}\n\n{persona.as_prompt_block()}\n\n"
                "Your own first-read reaction was:\n"
                f"{json.dumps(prior['reactions'], ensure_ascii=False)}\n\n"
                "You have now seen what other readers said (in the shared context above). "
                "Re-react. Update your stance ONLY if a SPECIFIC peer point genuinely moved "
                "you; if so, say which and why in shifted_because and set moved_toward. "
                "Otherwise hold your ground and set did_shift=false. Return your updated "
                "reaction(s) in the same shape as before."
            ),
        }
        return self._structured(
            config.NETWORK_MODEL, shared_system + [persona_block],
            "Given your prior reaction and your peers' reactions, react again.",
            NETWORK_SCHEMA, effort="high",
        )

    # --- synthesis -----------------------------------------------------------

    def synthesize(self, scope: str, reactions_digest: str) -> dict[str, Any]:
        if self.mock:
            return mock.synthesize()
        system = [{
            "type": "text",
            "text": (
                "You synthesize a synthetic reader panel's reactions into the headline "
                "deliverable: a RANKED OBJECTION LIST. Rank by how load-bearing the objection "
                "is for a big-idea nonfiction book's success. For each objection, tag it "
                "'confusing' (a clarity defect to FIX) or 'challenging' (a deliberate edge to "
                "PROTECT — do not sand it off). Reward provocation and distinctiveness; do not "
                "reward bland, hedged, safe prose. Also list what genuinely resonated. Be "
                "honest about disagreement; never average it away."
            ),
            "cache_control": {"type": "ephemeral"},
        }]
        user = f"Scope: {scope}\n\n=== PERSONA REACTIONS (weighted) ===\n{reactions_digest}"
        return self._structured(config.AGG_MODEL, system, user, SYNTH_SCHEMA,
                                thinking=True, effort="medium")


def _extract_citations(resp: Any) -> list[str]:
    """Best-effort provenance from web_search result blocks."""
    urls: list[str] = []
    try:
        for block in resp.content:
            for attr in ("citations",):
                for c in getattr(block, attr, None) or []:
                    u = getattr(c, "url", None)
                    if u:
                        urls.append(u)
            if "web_search" in getattr(block, "type", ""):
                for item in getattr(block, "content", None) or []:
                    u = getattr(item, "url", None)
                    if u:
                        urls.append(u)
    except Exception:
        pass
    # de-dup, stable order
    seen: set[str] = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out
