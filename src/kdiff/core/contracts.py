from __future__ import annotations

import calendar
import hashlib
import json
import re
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .schema import EntityType, FIELDS, IDENTITY_VERSION, SCHEMA_VERSION


def canonical(value: Any) -> bytes:
    def encode(obj):
        if isinstance(obj, BaseModel):
            return obj.model_dump(mode="json")
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        raise TypeError(f"Unsupported canonical value type: {type(obj).__name__}")
    return json.dumps(value, default=encode, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(namespace: str, kind: str, source_id: str) -> str:
    return f"{namespace}:{kind.lower()}:{uuid5(NAMESPACE_URL, canonical([namespace, kind, source_id]).decode())}"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TimeRange(Contract):
    """Date uncertainty bounds, not a fabricated exact point."""

    lower: date | None = None
    upper: date | None = None
    precision: Literal["day", "month", "year", "interval", "fuzzy", "unknown"] = "unknown"
    end_kind: Literal["bounded", "unknown", "ongoing"] = "unknown"
    raw: str | None = None

    @model_validator(mode="after")
    def valid(self):
        if self.lower and self.upper and self.lower > self.upper:
            raise ValueError("Reversed date bounds")
        if self.end_kind == "ongoing" and (not self.lower or self.upper):
            raise ValueError("Ongoing requires a known start and open end")
        if self.end_kind == "bounded" and (not self.lower or not self.upper):
            raise ValueError("Bounded requires both dates")
        if self.precision == "day" and self.lower != self.upper:
            raise ValueError("An exact day needs equal bounds")
        return self

    @classmethod
    def parse(cls, raw: str | int | None):
        if raw is None:
            return cls()
        s = str(raw)
        if re.fullmatch(r"\d{4}", s):
            lo, hi, precision = date(int(s), 1, 1), date(int(s), 12, 31), "year"
        elif re.fullmatch(r"\d{4}-\d{2}", s):
            y, m = map(int, s.split("-"))
            lo, hi, precision = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1]), "month"
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            lo = hi = date.fromisoformat(s)
            precision = "day"
        else:
            return cls(raw=s)  # Fuzzy prose stays unknown until evidenced normalization.
        return cls(lower=lo, upper=hi, precision=precision, raw=s, end_kind="bounded")


class Window(Contract):
    start: date
    end: date
    reference_date: date
    normalization: Literal["explicit_inclusive", "previous_ten_complete_years"] = "explicit_inclusive"

    @model_validator(mode="after")
    def valid(self):
        if self.start > self.end:
            raise ValueError("Reversed window")
        return self

    @classmethod
    def last_decade(cls, reference: date):
        return cls(start=date(reference.year - 10, 1, 1), end=date(reference.year - 1, 12, 31),
                   reference_date=reference, normalization="previous_ten_complete_years")


def membership(time: TimeRange, window: Window) -> Literal["inside", "outside", "indeterminate"]:
    if time.upper and time.upper < window.start or time.lower and time.lower > window.end:
        return "outside"
    if time.lower and time.upper and window.start <= time.lower <= time.upper <= window.end:
        return "inside"
    return "indeterminate"


class Provenance(Contract):
    source: str
    source_record_id: str
    source_version_or_hash: str
    raw_artifact_pointer: str
    source_path: str
    retrieved_at: str
    attested_at: str | None = None
    extraction_method: str = "openalex-json-v1"
    model_id: str | None = None
    model_revision: str | None = None
    prompt_hash: str | None = None
    schema_version: str = SCHEMA_VERSION
    confidence_score: float | None = Field(default=None, ge=0, le=1)
    calibration_status: Literal["not_calibrated", "measured"] = "not_calibrated"


class Entity(Contract):
    canonical_id: str
    namespace: str
    kind: EntityType
    subtype: Literal["site"] | None = None

    @model_validator(mode="after")
    def valid(self):
        if self.subtype and self.kind != EntityType.INSTITUTION:
            raise ValueError("Only institutions have site subtype")
        return self


class IdentityMapping(Contract):
    mapping_id: str
    source: str
    source_id: str
    canonical_id: str
    identity_map_version: str = IDENTITY_VERSION
    method: Literal["exact_source_id"] = "exact_source_id"
    person_identity_verified: bool = False
    evidence_hash: str


class Observation(Contract):
    observation_id: str
    entity_id: str
    kind: EntityType
    attributes: dict[str, Any]
    provenance: Provenance
    annotation: str = ""

    @model_validator(mode="after")
    def valid(self):
        if set(self.attributes) - set(FIELDS[self.kind]):
            raise ValueError("Unknown scientific attribute")
        if any(self.attributes.get(k) is not None for k in ("gender", "nationality")):
            raise ValueError("Demographic fields disabled in M1")
        return self


class Assertion(Contract):
    assertion_id: str
    logical_fact_key: str
    head_id: str
    relation: str
    tail_id: str
    valid_time: TimeRange
    observation_kind: Literal["publication", "employment", "announcement", "operation", "other"]
    provenance: Provenance
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    conflict_flag: bool = False
    annotation: str = ""
    identity_map_version: str = IDENTITY_VERSION


class Batch(Contract):
    namespace: str
    entities: list[Entity]
    observations: list[Observation]
    assertions: list[Assertion]
    identities: list[IdentityMapping]
    sources: list[dict[str, Any]]
    schema_version: str = SCHEMA_VERSION


class Label(StrEnum):
    SUPPORTED = "Supported"
    PARTIAL = "Partial"
    UNSUPPORTED = "Unsupported"
    CONTRADICTED = "Contradicted"
    AMBIGUOUS = "Ambiguous"
    NEI = "NEI"


class CountRequest(Contract):
    author_id: str
    window: Window
    policy: Literal["distinct_canonical_papers_all_assertions_strict_dates"] = "distinct_canonical_papers_all_assertions_strict_dates"


class ToolReceipt(Contract):
    run_id: str
    seed_id: str
    instance_id: str
    stage: str
    task_id: str
    tool: str
    arguments: dict[str, Any]
    result_hash: str | None = None
    status: Literal["success", "error"]
    error_type: str | None = None
    source_artifact_ids: list[str]
    model: str
    prompt_hash: str
    started_at: str
    finished_at: str
