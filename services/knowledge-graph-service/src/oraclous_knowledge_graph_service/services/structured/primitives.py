"""CSV + JSON structural primitives (services layer).

Reshaped from legacy `develop@84152635` csv/json primitives: operate on text (not a path) and — for
CSV — emit a synthetic TABLE container unit so the engine materialises a `:Table` node (the legacy
CSV primitive emitted only SOURCE+COLUMN+RECORD; the recon's option (a)). RECORD units carry the row
payload in `sample_values=[row_dict]`, which the engine reads for node identity + properties.
"""

from __future__ import annotations

from oraclous_knowledge_graph_service.domain.structural import (
    ExtractionMode,
    StructuralRepresentation,
    StructuralUnit,
    UnitKind,
)
from oraclous_knowledge_graph_service.services.structured.extractors import (
    extract_csv,
    extract_json,
    type_label,
)

_SAMPLE_LIMIT = 5
_SOURCE_ID = "source"
_TABLE_ID = "table:main"


class CsvPrimitive:
    source_type = "csv"

    def decompose(
        self, text: str, mode: ExtractionMode, *, name: str = "data.csv"
    ) -> StructuralRepresentation:
        extracted = extract_csv(text)
        columns: list[str] = extracted["columns"]
        schema: dict[str, str] = extracted["schema"]
        rows: list[dict[str, str]] = extracted["rows"]

        units: list[StructuralUnit] = [
            StructuralUnit(
                kind=UnitKind.SOURCE,
                unit_id=_SOURCE_ID,
                name=name,
                metadata={"row_count": extracted["row_count"], "column_count": len(columns)},
            ),
            StructuralUnit(kind=UnitKind.TABLE, unit_id=_TABLE_ID, name=name, parent_id=_SOURCE_ID),
        ]
        for col in columns:
            dt = schema.get(col, "str")
            units.append(
                StructuralUnit(
                    kind=UnitKind.COLUMN,
                    unit_id=f"column:{col}",
                    name=col,
                    data_type=dt,
                    role="free_text" if dt == "str" else None,
                    parent_id=_TABLE_ID,
                    sample_values=[r.get(col, "") for r in rows[:_SAMPLE_LIMIT] if col in r],
                )
            )
        record_rows = rows if mode == ExtractionMode.FULL else rows[:_SAMPLE_LIMIT]
        for idx, row in enumerate(record_rows):
            units.append(
                StructuralUnit(
                    kind=UnitKind.RECORD,
                    unit_id=f"record:{idx}",
                    name=f"row {idx}",
                    parent_id=_TABLE_ID,
                    sample_values=[dict(row)],
                )
            )
        signature = "csv(" + ",".join(f"{c}:{schema.get(c, 'str')}" for c in columns) + ")"
        return StructuralRepresentation(
            source_type=self.source_type, shape_signature=signature, mode=mode, units=units
        )


class JsonPrimitive:
    source_type = "json"

    def decompose(
        self, text: str, mode: ExtractionMode, *, name: str = "data.json"
    ) -> StructuralRepresentation:
        extracted = extract_json(text)
        records: list = extracted["records"]
        field_schema: dict = extracted["field_schema"]

        units: list[StructuralUnit] = [
            StructuralUnit(
                kind=UnitKind.SOURCE,
                unit_id=_SOURCE_ID,
                name=name,
                metadata={"record_count": extracted["record_count"]},
            )
        ]
        for key, ftype in field_schema.items():
            lbl = type_label(ftype)
            units.append(
                StructuralUnit(
                    kind=UnitKind.FIELD,
                    unit_id=f"field:{key}",
                    name=key,
                    data_type=lbl,
                    role="free_text" if lbl == "string" else None,
                    parent_id=_SOURCE_ID,
                    sample_values=[
                        r[key] for r in records[:_SAMPLE_LIMIT] if isinstance(r, dict) and key in r
                    ],
                )
            )
        record_list = records if mode == ExtractionMode.FULL else records[:_SAMPLE_LIMIT]
        for idx, record in enumerate(record_list):
            units.append(
                StructuralUnit(
                    kind=UnitKind.RECORD,
                    unit_id=f"record:{idx}",
                    name=f"record {idx}",
                    parent_id=_SOURCE_ID,
                    sample_values=[record],
                )
            )
        signature = (
            "json("
            + ",".join(f"{k}:{type_label(field_schema[k])}" for k in sorted(field_schema))
            + ")"
        )
        return StructuralRepresentation(
            source_type=self.source_type, shape_signature=signature, mode=mode, units=units
        )
