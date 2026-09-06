"""A converted app's form: what a model may propose, and how the filled-in fields reach the team
(domain layer, #938) — no I/O, no model.

A team declares exactly one input (``_declared_input_keys`` — the manifest's single
``task_input.key`` plus any member's ``fan_out.over``), so a converted app has no field names to
label. The owner ruled that a model INVENTS them, reading the team's own description and the
request text the run was actually started with, and the person edits them before the app is
saved. This module holds the two things that must be decidable without calling anything:

**What the model is allowed to have said.** A model returns whatever it likes; the form's contract
is narrow, and this module holds it rather than trusting the model to. A chatty model must not put
twelve fields on a colleague's screen (``MAX_FIELDS``), and a proposed field the console cannot
render is a curated refusal (``FormShapeError``) rather than a broken form.

**How the fields become one request.** The engine fail-closes on any input key the manifest does
not declare (``validate_input_keys``), so the invented fields cannot be sent as themselves — they
are joined into labelled lines (``fold``) and placed under the team's own declared key
(``to_run_inputs``). A field's NAME is therefore part of what the team reads, not decoration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

from oraclous_ohm._slug import basic_slug

#: The cap belongs to the ENDPOINT, not the model — a chatty answer still yields a usable form, and
#: refusing outright would send the person back to a run they cannot convert.
MAX_FIELDS: Final[int] = 8

_FIELD_TYPES: Final[frozenset[str]] = frozenset({"short_text", "long_text", "choice"})

_WHITESPACE = re.compile(r"\s+")


class FormShapeError(ValueError):
    """The model's proposed form does not fit the endpoint's contract.

    Always a curated 4xx at the service seam, never a 500 — a model answering in prose or naming a
    fourth field type is an expected outcome of asking a model, not a server fault.
    """


@dataclass(frozen=True)
class FormField:
    """One control the app's form draws. ``id`` is derived, never taken from the model — the
    filled-in values arrive keyed by id, and a model that repeated one would make one person's
    answer silently overwrite another's."""

    id: str
    name: str
    hint: str
    type: str
    options: list[str]
    example: str
    required: bool


def _collapse(value: Any) -> str:
    """Collapse any run of whitespace — a newline included — to one space, then trim.

    A name is written verbatim into the request as a line label (``fold`` below); a name spanning
    two lines would produce a line the author never wrote, which the joined preview in the save
    dialog could not honestly show.
    """
    if not isinstance(value, str):
        return ""
    return _WHITESPACE.sub(" ", value).strip()


def _unique_id(name: str, seen: set[str], index: int) -> str:
    """A stable handle derived from the name, never the model's own choice, de-duplicated against
    every id already taken in this draft. "Time frame" and "Time-frame" slugify the same and both
    still need their own id, or one person's answer overwrites the other's."""
    base = basic_slug(name) or f"field-{index + 1}"
    candidate = base
    n = 2
    while candidate in seen:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def parse_form_draft(payload: Any) -> list[FormField]:
    """Hold a model's proposed form to the endpoint's contract.

    ``payload`` is ``{"fields": [...]}``. Anything the screen could not render honestly —
    a field with no usable name, an unknown type, a choice with no options, a text field carrying
    options — raises ``FormShapeError``. More than ``MAX_FIELDS`` proposals are truncated rather
    than refused; an empty answer is refused, because an empty form is not a form.
    """
    if not isinstance(payload, dict):
        raise FormShapeError("a form draft must be a JSON object")
    raw = payload.get("fields")
    if not isinstance(raw, list) or not raw:
        raise FormShapeError("a form draft must propose at least one field")

    fields: list[FormField] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw[:MAX_FIELDS]):
        if not isinstance(item, dict):
            raise FormShapeError("each proposed field must be an object")

        name = _collapse(item.get("name"))
        if not name:
            raise FormShapeError("a field's name must not be blank")

        field_type = item.get("type")
        if field_type not in _FIELD_TYPES:
            raise FormShapeError(
                f"a field's type must be one of {sorted(_FIELD_TYPES)}, got {field_type!r}"
            )

        options_raw = item.get("options")
        if field_type == "choice":
            if not isinstance(options_raw, list) or not options_raw:
                raise FormShapeError("a 'choice' field carries at least one option")
            options = [str(o) for o in options_raw]
        else:
            if options_raw:
                raise FormShapeError("only a 'choice' field carries options")
            options = []

        field_id = _unique_id(name, seen_ids, index)
        seen_ids.add(field_id)
        fields.append(
            FormField(
                id=field_id,
                name=name,
                hint=item.get("hint") or "",
                type=field_type,
                options=options,
                example=item.get("example") or "",
                required=bool(item.get("required", False)),
            )
        )
    return fields


def fallback_fields(manifest: dict[str, Any]) -> list[FormField]:
    """The fallback when the model gives nothing usable, or is never called: exactly what #932
    already gave — one field carrying the team's own declared input, so a save can still complete.

    An empty list for a team that declares no input at all: there is nothing for a person to fill
    in, and the save still has to succeed — an app with an empty form simply runs the team as it
    was.
    """
    task_input = manifest.get("task_input")
    if not isinstance(task_input, dict) or not task_input.get("key"):
        return []
    return [
        FormField(
            id=str(task_input["key"]),
            name="Task",
            hint=task_input.get("description") or "",
            type="long_text",
            options=[],
            example="",
            required=bool(task_input.get("required", False)),
        )
    ]


def _split_lines(value: str) -> list[str]:
    """Every line break, not only the three from a keyboard.

    This is the indentation defence's whole surface, so it has to agree with what will later read
    the request. ``str.splitlines`` splits on U+2028, U+2029, U+0085, a vertical tab and a form feed
    as well as CR/LF — and so does a model. A hand-rolled CR/LF-only pattern left a value carrying
    one of those on the label's own line, unindented, where its second half started at column zero
    and read as another field: exactly the forgery the indentation exists to stop (security review).

    It also handles a Windows paste without leaving a stray carriage return on an indented line,
    which is the reason the hand-rolled version existed in the first place.
    """
    return value.splitlines()


def fold(fields: list[FormField], values: dict[str, Any]) -> str:
    """Join the filled-in fields into labelled lines, in the form's own stored order.

    A single-line value stays on the label's line (``"Name: value"``). A value that spans lines
    puts the label alone on its own line and indents every line of the value beneath it — nothing
    is discarded, and unlike a name (reviewed in the save dialog before storage) a value arrives
    later from whoever runs the app, with nobody looking at it first: indenting is what stops a
    line of the value being read as a second field, since only a label ever starts at column zero.

    A blank or whitespace-only value contributes no line at all — an unanswered optional field must
    not reach the team as an empty instruction. A value for an id the form does not declare is
    ignored: the stored form is the authority on what this app sends, never the request body.
    """
    lines: list[str] = []
    for f in fields:
        value = values.get(f.id)
        if not isinstance(value, str) or not value.strip():
            continue
        parts = _split_lines(value)
        if len(parts) == 1:
            lines.append(f"{f.name}: {value}")
        else:
            lines.append(f"{f.name}:")
            lines.extend(f"  {part}" for part in parts)
    return "\n".join(lines)


def missing_required(fields: list[FormField], values: dict[str, Any]) -> list[str]:
    """The NAMES of required fields left blank. Whitespace does not satisfy one — ``fold`` would
    drop it anyway, so a run that passed this check on whitespace would reach the team with the
    field simply absent."""
    missing: list[str] = []
    for f in fields:
        if not f.required:
            continue
        value = values.get(f.id)
        if not isinstance(value, str) or not value.strip():
            missing.append(f.name)
    return missing


def fan_out_keys(manifest: dict[str, Any]) -> set[str]:
    """The keys a team declares BESIDES its request — the ones ``to_run_inputs`` must carry rather
    than fold.

    A member that fans out declares the key it fans out over, and that key holds a LIST a person
    supplies. Folding it into the request would leave the member with nothing to fan out over, so
    it has to travel as itself. Naming the keys is the manifest reader's job: the run path had a
    ``passthrough`` parameter and no way to know what belonged in it, so it passed nothing and a
    fan-out team's list vanished on every run (found at code review).

    Both spellings the engine accepts are understood — ``$.regions`` and a bare ``regions`` — for
    the same reason ``_declared_input_keys`` understands both: a reader that knew only one would
    silently drop the other team's list.
    """
    keys: set[str] = set()
    for member in manifest.get("members") or []:
        if not isinstance(member, dict):
            continue
        fan_out = member.get("fan_out")
        if not isinstance(fan_out, dict):
            continue
        over = fan_out.get("over")
        if isinstance(over, str) and over:
            keys.add(over[2:] if over.startswith("$.") else over)
    return keys


def to_run_inputs(
    manifest: dict[str, Any],
    fields: list[FormField],
    values: dict[str, Any],
    passthrough: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fold the filled-in fields into ONE request under the team's own declared ``task_input.key``.

    The engine refuses any input key the manifest does not declare, so the invented fields can
    never be sent as themselves. ``passthrough`` carries fan-out keys (a member fanning out over a
    list needs the list, not prose) untouched. A manifest with no declared input yields an empty
    dict — there is nowhere to put the request, so nothing is sent.
    """
    inputs: dict[str, Any] = dict(passthrough or {})
    task_input = manifest.get("task_input")
    if isinstance(task_input, dict) and task_input.get("key"):
        inputs[str(task_input["key"])] = fold(fields, values)
    return inputs
