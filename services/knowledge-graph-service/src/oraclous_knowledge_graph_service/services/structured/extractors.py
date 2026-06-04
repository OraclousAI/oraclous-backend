"""CSV + JSON extraction (ORAA-4 §21 services layer).

Lifted from legacy `develop@84152635 knowledge-graph-builder/app/services/{csv_extractor,
json_extractor}.py`, adapted to operate on in-memory text (ingestion carries bytes, not a path).
Deterministic, stdlib-only (csv/json). Type inference: bool>int>float>date>str (CSV) and JSON's
native type vocabulary. Type sampling caps at the first 100 rows/records (legacy behaviour).
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime
from typing import Any

_TYPE_SAMPLE_CAP = 100


# --- CSV ---------------------------------------------------------------------
def _infer_type(values: list[str]) -> str:
    non_empty = [v.strip() for v in values if v and v.strip()]
    if not non_empty:
        return "str"

    def is_bool(v: str) -> bool:
        return v.lower() in {"true", "false", "yes", "no", "1", "0"}

    def is_int(v: str) -> bool:
        try:
            int(v)
            return True
        except ValueError:
            return False

    def is_float(v: str) -> bool:
        try:
            float(v)
            return True
        except ValueError:
            return False

    def is_date(v: str) -> bool:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d"):
            try:
                date.fromisoformat(v) if fmt == "%Y-%m-%d" else datetime.strptime(v, fmt)
                return True
            except ValueError:
                continue
        return False

    if all(is_bool(v) for v in non_empty):
        return "bool"
    if all(is_int(v) for v in non_empty):
        return "int"
    if all(is_float(v) for v in non_empty):
        return "float"
    if all(is_date(v) for v in non_empty):
        return "date"
    return "str"


def _detect_delimiter(text: str) -> str:
    try:
        return csv.Sniffer().sniff(text[:4096], delimiters=",\t;|").delimiter
    except csv.Error:
        return ","


def extract_csv(text: str) -> dict[str, Any]:
    delimiter = _detect_delimiter(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    columns = list(reader.fieldnames or [])
    rows: list[dict[str, str]] = [dict(r) for r in reader]
    samples: dict[str, list[str]] = {c: [] for c in columns}
    for row in rows[:_TYPE_SAMPLE_CAP]:
        for col in columns:
            samples[col].append(row.get(col, "") or "")
    schema = {col: _infer_type(samples[col]) for col in columns}
    return {"columns": columns, "row_count": len(rows), "rows": rows, "schema": schema}


# --- JSON --------------------------------------------------------------------
def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _infer_schema(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return _type_name(obj)
    if isinstance(obj, dict):
        return {k: _infer_schema(v) for k, v in obj.items()}
    if isinstance(obj, list):
        if not obj:
            return {"type": "array", "items": "unknown"}
        item_schemas = [_infer_schema(item) for item in obj[:10]]
        if all(isinstance(s, dict) for s in item_schemas):
            merged: dict[str, Any] = {}
            for schema in item_schemas:
                for key, val in schema.items():
                    merged.setdefault(key, val)
            return {"type": "array", "items": merged}
        return {"type": "array", "items": item_schemas[0]}
    return "unknown"


def _merge_schemas(a: Any, b: Any) -> Any:
    if isinstance(a, dict) and isinstance(b, dict):
        merged = dict(a)
        for key, val in b.items():
            merged[key] = _merge_schemas(merged[key], val) if key in merged else val
        return merged
    if a == b:
        return a
    if a == "unknown":
        return b
    if b == "unknown":
        return a
    return a


def type_label(ftype: Any) -> str:
    if isinstance(ftype, str):
        return ftype
    if isinstance(ftype, dict) and ftype.get("type") == "array":
        return "array"
    if isinstance(ftype, dict):
        return "object"
    return "unknown"


def _parse_records(text: str) -> list[Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        records: list[Any] = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                try:
                    records.append(json.loads(stripped))
                except json.JSONDecodeError:
                    pass
        return records
    return data if isinstance(data, list) else [data]


def extract_json(text: str) -> dict[str, Any]:
    records = _parse_records(text)
    schema: Any = {}
    for record in records[:_TYPE_SAMPLE_CAP]:
        schema = _merge_schemas(schema, _infer_schema(record))
    if isinstance(schema, dict) and schema.get("type") == "array":
        field_schema = schema.get("items", {})
    elif isinstance(schema, dict):
        field_schema = schema
    else:
        field_schema = {}
    if not isinstance(field_schema, dict):
        field_schema = {}
    return {"records": records, "record_count": len(records), "field_schema": field_schema}
