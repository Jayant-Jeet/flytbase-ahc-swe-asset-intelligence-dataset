"""render_visual: schema-validated visual specs the UI draws inline.

The model supplies a spec; this module only validates and normalises it (no facts are computed here).
Errors are raised as ValueError with a precise message so the model can correct and retry.
"""
from __future__ import annotations

import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

VISUAL_TYPES = ("status_card", "table", "line_chart", "bar_chart", "map", "heatmap")
MAX_ROWS, MAX_POINTS = 500, 2000

Scalar = Any


class Field_(BaseModel):
    label: str
    value: Scalar


class StatusCard(BaseModel):
    well_id: Optional[str] = None
    headline: str
    producing: Optional[bool] = None
    fields: list[Field_] = []
    notes: list[str] = []


class Table(BaseModel):
    columns: list[str] = Field(min_length=1)
    rows: list[list[Scalar]]

    @model_validator(mode="after")
    def _shape(self):
        if len(self.rows) > MAX_ROWS:
            raise ValueError(f"table has {len(self.rows)} rows; max {MAX_ROWS}")
        for i, r in enumerate(self.rows):
            if len(r) != len(self.columns):
                raise ValueError(f"row {i} has {len(r)} cells but there are {len(self.columns)} columns")
        return self


class Point(BaseModel):
    x: Scalar
    y: Optional[float] = None

    @field_validator("y", mode="before")
    @classmethod
    def _num(cls, v):
        if v is None or isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
        raise ValueError("point y must be a number or null")


class Series(BaseModel):
    name: str
    points: list[Point]


class Chart(BaseModel):
    series: list[Series] = Field(min_length=1)
    stacked: bool = False

    @model_validator(mode="after")
    def _size(self):
        if sum(len(s.points) for s in self.series) > MAX_POINTS:
            raise ValueError(f"chart has too many points (max {MAX_POINTS})")
        return self


class Marker(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    label: str
    status: Optional[Literal["producing", "not_producing", "unknown", "alert"]] = None
    detail: Optional[str] = None


class MapData(BaseModel):
    markers: list[Marker] = Field(min_length=1)


class Heatmap(BaseModel):
    x_labels: list[str] = Field(min_length=1)
    y_labels: list[str] = Field(min_length=1)
    values: list[list[Optional[float]]]

    @model_validator(mode="after")
    def _shape(self):
        if len(self.values) != len(self.y_labels):
            raise ValueError("values needs one row per y_label")
        for i, r in enumerate(self.values):
            if len(r) != len(self.x_labels):
                raise ValueError(f"values row {i} needs {len(self.x_labels)} cells (one per x_label)")
        return self


class Spec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: Literal["status_card", "table", "line_chart", "bar_chart", "map", "heatmap"]
    title: str = Field(min_length=1)
    data: dict
    x_label: Optional[str] = None
    y_label: Optional[str] = None
    subtitle: Optional[str] = None


_DATA = {"status_card": StatusCard, "table": Table, "line_chart": Chart, "bar_chart": Chart, "map": MapData,
         "heatmap": Heatmap}


def validate_spec(spec: Any) -> dict:
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except json.JSONDecodeError as e:
            raise ValueError(f"spec is not valid JSON: {e}") from e
    if not isinstance(spec, dict):
        raise ValueError("spec must be a JSON object with type, title, data")
    try:
        s = Spec.model_validate(spec)
        data = _DATA[s.type].model_validate(s.data)
    except ValidationError as e:
        msgs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:6])
        raise ValueError(f"invalid {spec.get('type')!r} spec: {msgs}. Allowed types: {list(VISUAL_TYPES)}") from e
    out = s.model_dump(exclude={"data"}, exclude_none=True)
    out["data"] = data.model_dump(exclude_none=False)
    return out


def render_visual(conn, spec: Any) -> dict:
    return {"ok": True, "visual": validate_spec(spec),
            "note": "Rendered in the chat UI. Do not repeat its contents in the answer; summarise the finding in a sentence."}


PARAMS = {
    "type": "object",
    "properties": {"spec": {"type": "string", "description": (
        "JSON string: {type, title, x_label?, y_label?, data}. type is one of status_card | table | line_chart | "
        "bar_chart | map | heatmap. data by type: "
        "status_card {well_id, headline, producing(true/false/null), fields:[{label,value}], notes:[str]}; "
        "table {columns:[str], rows:[[...]]}; "
        "line_chart/bar_chart {series:[{name, points:[{x,y}]}], stacked?:true} (x = date or category, y = number copied from a tool result; "
        "stacked:true on bar_chart stacks the series, e.g. delay reasons per area); "
        "map {markers:[{lat,lon,label,status?:producing|not_producing|unknown|alert,detail?}]}; "
        "heatmap {x_labels:[str], y_labels:[str], values:[[number]] (one row per y label)}. "
        "Use only numbers returned by tools.")}},
    "required": ["spec"],
}
