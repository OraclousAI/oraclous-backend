"""Deterministic offline generators so the full pipeline can run without an API
key. `--mock` exercises corpus -> personas -> reactions -> network -> report
end to end with canned-but-plausible data. NOT for real signal."""
from __future__ import annotations

import hashlib
from typing import Any


def _h(*parts: Any) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest(), 16)


def themes(title: str, author: str) -> tuple[str, list[str]]:
    md = (
        f"# Reader themes (MOCK) — {title} by {author}\n\n"
        "## Resonance\n- Readers praise the big sweep and vivid framing.\n"
        "- The historical examples land.\n\n"
        "## Complaint\n- Too dense in the middle third.\n"
        "- Hand-waves the hard problem of consciousness.\n"
        "- Ends without practical takeaway.\n\n"
        "## Emotion\n- Awe early, overwhelm by the end; some report anxiety.\n\n"
        "## Reader archetypes observed\n- Skeptical domain expert; casual-curious; "
        "literary-nonfiction enthusiast; contrarian reviewer.\n"
    )
    return md, [f"https://example.org/mock/{title.replace(' ', '-').lower()}"]


def archetypes(n: int) -> list[dict[str, Any]]:
    base = [
        dict(
            archetype_name="The Skeptical Domain Expert",
            panel_weight=0.20,
            backstory="I'm a cognitive-science postdoc; I argue with big-idea books in the margins.",
            reading_habits="reads in 30-min train sessions; DNFs by ch.3 if hand-wavy",
            reading_level="specialist on consciousness; general on AI policy",
            jtbd="see the mechanism before I trust the rest of the book",
            prior_beliefs="deflationary about 'digital consciousness'; allergic to hype",
            value_prior="rigor over warmth",
            resonance="precise definitions; steelmanning the opposing view",
            complaint="hand-waves the hard problem; conflates intelligence with consciousness",
            annoyances=["AI-tells / GPT cadence", "undefined jargon", "false balance"],
            voice_sample="He gestures at IIT and moves on. Which is it?",
            devils_advocate=True,
        ),
        dict(
            archetype_name="The Casual-Curious Reader",
            panel_weight=0.30,
            backstory="I picked this up after a podcast; I read before bed and want to feel smarter, not lectured.",
            reading_habits="15 minutes a night; skips footnotes",
            reading_level="general; no AI background",
            jtbd="understand what AI means for my life without a CS degree",
            prior_beliefs="vaguely worried, mostly curious",
            value_prior="clarity and a story over completeness",
            resonance="a vivid example that makes an abstract idea click",
            complaint="loses me when it gets technical with no payoff",
            annoyances=["jargon", "long tangents", "doom with no hope"],
            voice_sample="Wait, but what does that actually mean for me?",
            devils_advocate=False,
        ),
        dict(
            archetype_name="The Literary-Nonfiction Enthusiast",
            panel_weight=0.30,
            backstory="I read Harari and Gladwell for the prose and the ideas; voice matters to me.",
            reading_habits="reads in long weekend sittings; underlines sentences",
            reading_level="well-read generalist",
            jtbd="a big idea delivered with craft I can quote to friends",
            prior_beliefs="open; rewards a bold thesis",
            value_prior="voice and structure as much as argument",
            resonance="a sentence that reframes how I see something",
            complaint="competent but flat; reads like a report, not a voice",
            annoyances=["AI-flat prose", "listicle structure", "no narrative spine"],
            voice_sample="The idea's there, but where's the music?",
            devils_advocate=False,
        ),
        dict(
            archetype_name="The Contrarian Reviewer",
            panel_weight=0.20,
            backstory="I write sharp Goodreads reviews; I look for the overclaim and the missing counterargument.",
            reading_habits="reads fast, hunting for the weak link",
            reading_level="generalist with strong opinions",
            jtbd="find what the author got wrong before recommending it",
            prior_beliefs="suspicious of consensus tech narratives",
            value_prior="intellectual honesty; hates being sold to",
            resonance="an author who names the strongest objection to their own thesis",
            complaint="cherry-picked evidence; ignores the obvious counterargument",
            annoyances=["TED-talk optimism", "unfalsifiable predictions", "strawmen"],
            voice_sample="Convenient that the one counterexample never comes up.",
            devils_advocate=True,
        ),
    ]
    out: list[dict[str, Any]] = []
    i = 0
    while len(out) < n:
        proto = dict(base[i % len(base)])
        copy_idx = i // len(base)
        proto["persona_id"] = (
            proto["archetype_name"].lower().replace("the ", "").replace(" ", "-")
            + f"-{copy_idx + 1:02d}"
        )
        proto["provenance"] = "MOCK corpus cluster; not real review data"
        out.append(proto)
        i += 1
    # renormalize weights across the actual roster
    total = sum(p["panel_weight"] for p in out)
    for p in out:
        p["panel_weight"] = round(p["panel_weight"] / total, 4)
    return out


_TEMPLATES = [
    dict(
        gut_reaction="Mostly engaged; one section dragged.",
        pulled_in_at="the opening scene",
        put_down_at="the middle technical stretch",
        delighted_by="the central reframing",
        annoyed_by="a leap that wasn't earned",
        strongest_objection="the consciousness claim is asserted, not argued",
        one_paragraph_review="A strong sweep with a soft middle; I wanted the mechanism.",
        would_recommend_to="curious friends, with a caveat",
        emotion_felt="curiosity/dopamine",
    ),
    dict(
        gut_reaction="Bounced off the abstraction.",
        pulled_in_at="the human anecdote",
        put_down_at="the second definitions section",
        delighted_by="the one vivid example",
        annoyed_by="jargon without payoff",
        strongest_objection="too much told, not shown",
        one_paragraph_review="Promising but uneven; the ideas outrun the prose here.",
        would_recommend_to="no one yet",
        emotion_felt="clarity/serotonin+oxytocin",
    ),
    dict(
        gut_reaction="Quietly impressed by the back half.",
        pulled_in_at="the late synthesis",
        put_down_at="never, once it found its footing",
        delighted_by="the way the threads tie together",
        annoyed_by="a slow start",
        strongest_objection="the early chapters bury the lede",
        one_paragraph_review="Sticks the landing; trust it past the opening.",
        would_recommend_to="anyone who liked the comps",
        emotion_felt="awe/dopamine",
    ),
]


def _stars(persona_id: str, salt: str, n: int = 3) -> list[dict[str, Any]]:
    seed = _h(persona_id, salt)
    contrarian = "expert" in persona_id or "contrarian" in persona_id
    center = 3 if contrarian else 4
    n = max(1, n)
    weights = [0.5, 0.3, 0.2, 0.1, 0.05][:n]
    wtot = sum(weights)
    out: list[dict[str, Any]] = []
    for i in range(n):
        star = max(1, min(5, center + ((seed >> (i * 2)) % 3) - 1))
        t = dict(_TEMPLATES[i % len(_TEMPLATES)])
        t["star_rating"] = star
        t["probability"] = round(weights[i] / wtot, 4)
        out.append(t)
    return out


def react(persona_id: str, samples: int = 3) -> dict[str, Any]:
    return {"reactions": _stars(persona_id, "r1", samples)}


def network_react(persona_id: str, prior: dict[str, Any]) -> dict[str, Any]:
    shift = (_h(persona_id, "net") % 3) == 0
    updated = [dict(s) for s in prior["reactions"]]  # copy; don't mutate round 1
    if shift and updated:
        # word-of-mouth actually moves the rating (real runs do this via the model)
        top = max(updated, key=lambda s: s.get("probability", 0))
        top["star_rating"] = max(1, int(top["star_rating"]) - 1)
    return {
        "updated": updated,
        "did_shift": shift,
        "shifted_because": "a peer pointed out the missing counterargument" if shift else "",
        "moved_toward": "more_negative" if shift else "none",
    }


def synthesize() -> dict[str, Any]:
    return {
        "ranked_objections": [
            dict(
                objection="The central consciousness claim is asserted, not argued.",
                severity="high",
                frequency_signal="raised by the expert and contrarian segments",
                segments_affected=["The Skeptical Domain Expert", "The Contrarian Reviewer"],
                tag="challenging",
                fix_or_protect="protect",
                representative_quote="He gestures at IIT and moves on. Which is it?",
            ),
            dict(
                objection="The middle technical stretch loses general readers.",
                severity="medium",
                frequency_signal="raised across casual and enthusiast segments",
                segments_affected=["The Casual-Curious Reader", "The Literary-Nonfiction Enthusiast"],
                tag="confusing",
                fix_or_protect="fix",
                representative_quote="loses me when it gets technical with no payoff",
            ),
        ],
        "what_resonated": [
            "The opening reframing landed across every segment.",
            "One vivid human example carried the abstract idea.",
        ],
        "summary": "MOCK synthesis: strong open, soft technical middle, a defensible-but-"
        "unargued consciousness claim. Protect the edge; fix the middle's clarity.",
    }
