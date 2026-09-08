"""Website addresses, cleaned to one canonical form (shared kernel, #951 / #961).

One rule, three services, because three of them now have to agree on what one address means.

* The **capability registry** cleans each entry before it reaches the search vendor. #951's live
  probe (2026-09-07) established why that is correctness and not tidiness: the vendor accepts a full
  URL in its domain restriction with an ordinary 200 and then silently drops the restriction
  entirely, so a pasted address sent through produces an unrestricted search that looks completely
  normal.
* The **execution engine** cleans what a person typed into an app form's website box, so the run
  carries addresses rather than whatever shape their browser handed them.
* The **harness runtime** cleans the ``sites`` argument a model wrote, to check it against the
  person's list before the search is dispatched (#961 ruling 2).

That last one is why this lives here rather than in the registry. A person pastes
``https://www.theverge.com/``; a model calls the tool with ``theverge.com``. Those are the same
site, and a check that said otherwise would refuse a search that was perfectly correct. Two copies
of this rule put that disagreement between two SERVICES — #946 already shipped the two-function
version of that defect and it cost a review round to find.

**The clean is a fixed point**: applying it twice provably equals applying it once. That property is
what makes it safe for three callers to each clean the value they hold and still meet.

**A name is never turned into an address.** ``BBC News`` does not become ``bbc.co.uk``. #951 ruled
there is no name-to-hostname table anywhere, because a plausible wrong guess (``bbc.com`` for
``bbc.co.uk``) cannot be told from a right one — both resolve and both return real pages — and #963
is the record of a model inventing them anyway, eight times out of eight.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

#: How many websites one search may name. Tavily's own ceiling is 300 and going over it is a 400;
#: ours sits well below because a person filling in a "which sites" box names a handful, and a
#: runaway list is a mistake worth refusing near where it was made rather than at the vendor.
MAX_SITES = 20
_MAX_HOSTNAME_CHARS = 253
#: Two or more labels, each 1-63 chars of ``[a-z0-9-]`` and never hyphen-edged. Deliberately strict:
#: an underscore, an empty label or a single label (``localhost``) is refused here rather than at
#: the vendor, so the refusal can name the offending value in a sentence the caller can act on, and
#: so no upstream body has to be echoed to explain it (ADR-008).
_HOSTNAME_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")


class InvalidSiteError(ValueError):
    """A caller named a site that is not a website address, or named too many (#951).

    Distinct from :class:`SearchProviderError`: nothing went wrong upstream, the ARGUMENT is wrong.
    The connector maps it to the same ``INVALID_INPUT`` a missing ``query`` already gets, so no new
    entry in the gateway's error taxonomy is needed for it to reach a person.
    """


#: How much of an offending value a refusal quotes back. The message names the value so the caller
#: can fix THAT one, but the value is caller-supplied and unbounded — a model can send a megabyte —
#: and this message travels into a run's error text and a person's screen. So it is bounded here,
#: at the one place a caller-supplied value enters a message.
_SHOWN_VALUE_CHARS = 120
#: A ``user:password@`` prefix inside a pasted address. Matched before any parsing, because the
#: value being quoted is precisely the one that FAILED to parse.
_USERINFO_RE = re.compile(r"[^\s/@]*@")


def _shown(value: object) -> str:
    """A caller-supplied value, rendered safe and short enough to sit inside an error message.

    A refusal is written down: it becomes the execution row's ``error_message`` and is rendered on
    a person's screen. So it must never carry a secret the caller pasted. A person copying an
    address out of their browser can bring a ``user:password@`` prefix or a ``?token=`` query with
    it — the ACCEPT path already drops both, because only the hostname is ever sent, but the
    REFUSE path is the one that writes the value down, so it has to drop them too.

    What survives is the part that has to change for the call to work, which is what the caller
    needs to see.
    """
    text = value if isinstance(value, str) else repr(value)
    text = text.split("?", 1)[0].split("#", 1)[0]  # a query or fragment can carry a token
    text = _USERINFO_RE.sub("", text)
    if len(text) > _SHOWN_VALUE_CHARS:
        return f"{text[:_SHOWN_VALUE_CHARS]}…"
    return text


def _hostname_of(entry: str) -> str:
    """One trimmed entry → a bare lowercase hostname, or raise :class:`InvalidSiteError`.

    Accepts every form a person actually supplies — ``theverge.com``, ``www.theverge.com``,
    ``https://theverge.com/tech`` — because they will paste whichever their browser gave them, and
    a URL that reaches the vendor is silently ignored rather than refused.
    """
    if not entry:
        raise InvalidSiteError("a website address cannot be blank")
    if any(ch.isspace() for ch in entry):
        # A hostname never contains a space, so this is almost always several sites run together
        # ("theverge.com and bbc.co.uk"). Keeping the first and dropping the rest would be the same
        # silent-loss bug in a new place, so it is refused and named instead.
        raise InvalidSiteError(
            f"'{_shown(entry)}' is not a single website address — give one address per entry, "
            "with nothing else in it, like theverge.com"
        )
    # A bare hostname has no scheme, so `urlsplit` would read it as a path. Prefixing `//` makes it
    # parse as an authority; a value that already carries `://` is left alone, so `file:///…`
    # resolves to no host at all. A scheme without `//` (`javascript:alert(1)`) does get the prefix
    # and parses to a single-label host, which the pattern below refuses.
    try:
        host = urlsplit(entry if "://" in entry else f"//{entry}").hostname
    except ValueError as exc:  # a malformed authority (an unclosed IPv6 bracket, say)
        raise InvalidSiteError(f"'{_shown(entry)}' is not a website address") from exc
    if not host:
        raise InvalidSiteError(f"'{_shown(entry)}' is not a website address")
    host = host.rstrip(".")  # a fully-qualified trailing dot is the same host
    if not host.isascii():
        # An internationalised name reaches the vendor in its ASCII form. The codec also enforces
        # the label-length rules, so a name it refuses never reaches the regex below.
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise InvalidSiteError(f"'{_shown(entry)}' is not a website address") from exc
    while host.startswith("www.") and host.count(".") > 1:
        # `www.` is a subdomain the vendor treats as equivalent; dropping it is what makes the three
        # forms of one address collapse to a single value. Guarded so `www.com` is not reduced to a
        # single label that then fails for a confusing reason.
        #
        # A LOOP, not an `if`: this function is applied twice — once by the connector to report what
        # it searched, once at the vendor hop — and stripping only one label per call would make
        # those two passes disagree. `www.www.theverge.com` reported as `www.theverge.com` while
        # `theverge.com` was actually sent is a report that misstates the search that ran, which is
        # the dishonesty this whole issue exists to remove. Looping makes the clean a fixed point,
        # so applying it twice provably equals applying it once.
        host = host[4:]
    if len(host) > _MAX_HOSTNAME_CHARS or not _HOSTNAME_RE.match(host):
        raise InvalidSiteError(f"'{_shown(entry)}' is not a website address, like theverge.com")
    if not any(ch.isalpha() for ch in host.rsplit(".", 1)[-1]):
        # An all-digit last label means an IP address, which has no domain suffix — the vendor 400s
        # it, and restricting a web search to a bare address is never what someone meant.
        raise InvalidSiteError(
            f"'{_shown(entry)}' is an address literal, not a website, like theverge.com"
        )
    return host


def normalise_sites(value: object) -> list[str]:
    """Caller-supplied sites → the bare hostnames to send, in the order given, without duplicates.

    ``None``, an empty list and a blank string all mean "do not restrict", which the provider turns
    into byte-for-byte the request it sent before this argument existed.

    A bare string is SPLIT ON COMMAS rather than ignored (ruled 2026-09-08). A model that sends
    ``"theverge.com, bbc.co.uk"`` is asking for a restriction in the wrong container, and dropping
    that silently is #951's own bug in a new place. Accepting it is safe only because the run
    reports the cleaned hostnames back, so a misreading is visible rather than hidden. Anything that
    is neither a list nor a string is refused — there is no unambiguous reading of it.
    """
    if value is None:
        entries: list[object] = []
    elif isinstance(value, str):
        entries = [value]
    elif isinstance(value, list):
        entries = list(value)
    else:
        raise InvalidSiteError(
            "'sites' must be a list of website addresses, like ['theverge.com', 'bbc.co.uk']"
        )
    cleaned: list[str] = []
    named = 0
    for entry in entries:
        if not isinstance(entry, str):
            raise InvalidSiteError(
                f"'{_shown(entry)}' is not a website address — "
                "each entry is text, like theverge.com"
            )
        for part in entry.split(","):
            part = part.strip()
            if not part:
                continue  # a trailing comma or a blank box is not an error, it is nothing asked for
            named += 1
            if named > MAX_SITES:
                # Refused, never trimmed: quietly dropping sites someone named is the same class of
                # bug as the URL the vendor ignores. Refused HERE, on the count named rather than on
                # the deduplicated total, so a runaway list costs one comparison rather than a
                # hostname parse per entry — the caller controls how long this list is.
                raise InvalidSiteError(f"too many websites named — at most {MAX_SITES} per search")
            host = _hostname_of(part)
            if host not in cleaned:
                cleaned.append(host)
    return cleaned
