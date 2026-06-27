"""Corpus stage: gather reader-review *themes* for the niche's comp titles.

Sourcing (per the author's choice): free + web-search crawl. We gather paraphrased
THEMES via the model's web_search tool, never bulk verbatim reviews — that keeps us
clear of TOS/PII/copyright while still grounding personas in real reader signal. We
also fold in any existing calib-reader-voice / calib-audience artifacts so the panel
reuses Team ①'s work instead of redoing it.

Outputs land in research/raw/panel-corpus/ which is append-only ground evidence
(AGENTS.md §3). Provenance (source URLs, access date) is logged per comp.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from . import config
from .llm import Client


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _scrub(md: str) -> str:
    """Defense-in-depth over the prompt instruction: research/raw/ is permanent, so
    redact obvious PII (emails, @handles) and collapse any long verbatim-looking
    quoted span before the themes corpus is written. Heuristic, not a guarantee."""
    md = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[email-redacted]", md)
    md = re.sub(r"(?<!\w)@[A-Za-z0-9_]{2,}", "[handle-redacted]", md)
    # any quoted run longer than ~240 chars is more "verbatim review" than "theme"
    md = re.sub(r'"([^"]{240,})"', '"[long quote trimmed — paraphrase themes only]"', md)
    return md


def gather(client: Client, cfg: config.RunConfig,
           comps: list[tuple[str, str]] | None = None) -> str:
    """Gather themes for each comp, write provenance-logged corpus files, and
    return the combined corpus text used to mine personas."""
    comps = comps or config.COMP_TITLES
    paths = config.Paths()
    paths.ensure()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    chunks: list[str] = []

    for title, author in comps:
        md, citations = client.gather_themes(title, author)
        md = _scrub(md)
        prov = (
            f"<!-- provenance: comp='{title}' author='{author}' "
            f"access_date={stamp} method=web_search "
            f"sources={citations} note='paraphrased themes only; no verbatim reviews' -->\n"
        )
        out_path = paths.corpus_dir / f"{_slug(title)}.md"
        out_path.write_text(prov + md + "\n", encoding="utf-8")
        chunks.append(f"## {title} — {author}\n{md}")

    # Reuse existing Team ① calibration artifacts if the author has run them.
    reused = _reuse_calibration(paths)
    if reused:
        chunks.append("## Reused Team ① calibration artifacts\n" + reused)

    return "\n\n".join(chunks)


def _reuse_calibration(paths: config.Paths) -> str:
    if not paths.calibration_dir.exists():
        return ""
    parts: list[str] = []
    for pat in ("reader-voice-*.md", "audience-*.md", "*reader-voice*.md", "*audience*.md"):
        for f in sorted(paths.calibration_dir.glob(pat)):
            try:
                parts.append(f"### {f.name}\n{f.read_text(encoding='utf-8')[:6000]}")
            except Exception:
                continue
    # de-dup by filename
    seen: set[str] = set()
    uniq = []
    for p in parts:
        head = p.split("\n", 1)[0]
        if head not in seen:
            seen.add(head)
            uniq.append(p)
    return "\n\n".join(uniq)


def load_existing_corpus() -> str:
    """Re-read the on-disk corpus (so `personas` can run without re-gathering)."""
    paths = config.Paths()
    if not paths.corpus_dir.exists():
        return ""
    chunks = []
    for f in sorted(paths.corpus_dir.glob("*.md")):
        chunks.append(f.read_text(encoding="utf-8"))
    extra = _reuse_calibration(paths)
    if extra:
        chunks.append(extra)
    return "\n\n".join(chunks)
