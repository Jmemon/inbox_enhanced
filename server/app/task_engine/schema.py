"""EPS (Entity-Pipeline Schema) language.

The contract a `tasks.state_schema` JSONB column must satisfy. This module is
intentionally pure — pydantic only, no database imports — because it is
shared by three consumers that must never see conflicting rules: the LLM
propose flow (which needs a human-readable error to retry against), the
extraction validator (Task 6, which mechanically checks LLM-proposed
transitions against `all_stages()`/`attr()`), and the future board UI (which
renders stages/attributes straight off this shape).

Vocabulary:
- A task tracks either one implicit singleton entity (`entity=None`, keyed by
  `SINGLETON_KEY` in state storage — e.g. "track my visa application") or
  multiple named entities of one noun (`entity=EntitySpec`, e.g. "company"
  for a job hunt).
- Every task has exactly one pipeline: an ordered list of non-terminal
  `stages` plus a disjoint set of `terminal` stages (accepted/rejected/done).
- Entities may carry typed `attributes` (string/number/datetime/boolean/enum)
  in addition to the reserved `stage` field.

Fixed rules encoded here (not configurable flags): forward/lateral/skip stage
moves are always allowed at the schema level — backward moves and terminal
exits are the extraction *validator's* job (Task 6), not a schema flag.
"""

from __future__ import annotations

import math
from datetime import datetime

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

ATTR_TYPES = {"string", "number", "datetime", "boolean", "enum"}
RESERVED_FIELD = "stage"
SINGLETON_KEY = "_self"

# A few forgiving, unambiguous datetime input formats beyond ISO-8601, tried
# in order after datetime.fromisoformat fails. Kept short on purpose — this
# is meant to smooth over common LLM/user output, not parse every calendar
# format on earth.
_DATETIME_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y",
    "%m/%d/%Y %H:%M",
)


def _duplicates(items: list[str]) -> list[str]:
    """Return the subset of items that occur more than once, order-stable."""
    seen: set[str] = set()
    dupes: list[str] = []
    for item in items:
        if item in seen and item not in dupes:
            dupes.append(item)
        seen.add(item)
    return dupes


class AttributeSpec(BaseModel):
    """A typed, entity-level field beyond the reserved `stage` field."""

    key: str
    type: str
    values: list[str] | None = None  # required iff type == "enum"

    @field_validator("key")
    @classmethod
    def _key_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("attribute key must be non-empty")
        if v == RESERVED_FIELD:
            raise ValueError(
                f"attribute key '{RESERVED_FIELD}' is reserved for the pipeline stage"
            )
        return v

    @field_validator("type")
    @classmethod
    def _type_valid(cls, v: str) -> str:
        if v not in ATTR_TYPES:
            raise ValueError(
                f"attribute type '{v}' is not one of {sorted(ATTR_TYPES)}"
            )
        return v

    @model_validator(mode="after")
    def _values_match_enum_requirement(self) -> "AttributeSpec":
        if self.type == "enum":
            if not self.values:
                raise ValueError(
                    f"attribute '{self.key}' has type 'enum' but no 'values' list"
                )
        elif self.values:
            raise ValueError(
                f"attribute '{self.key}' has type '{self.type}' but sets 'values' "
                "(only 'enum' attributes may have values)"
            )
        return self


class EntitySpec(BaseModel):
    """The single noun a multi-entity task tracks, e.g. 'company'."""

    noun: str
    identity_hint: str = ""
    attributes: list[AttributeSpec] = Field(default_factory=list)

    @field_validator("noun")
    @classmethod
    def _noun_non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("entity noun must be non-empty")
        return v

    @model_validator(mode="after")
    def _unique_attribute_keys(self) -> "EntitySpec":
        dupes = _duplicates([a.key for a in self.attributes])
        if dupes:
            raise ValueError(f"duplicate attribute key(s): {', '.join(dupes)}")
        return self


class PipelineSpec(BaseModel):
    """Ordered non-terminal stages plus a disjoint set of terminal stages."""

    stages: list[str]
    terminal: list[str] = Field(default_factory=list)

    @field_validator("stages")
    @classmethod
    def _stages_valid(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("pipeline must declare at least one stage")
        cleaned = [s.strip() if isinstance(s, str) else s for s in v]
        if any(not s for s in cleaned):
            raise ValueError("pipeline stage names must be non-empty")
        dupes = _duplicates(cleaned)
        if dupes:
            raise ValueError(f"duplicate pipeline stage(s): {', '.join(dupes)}")
        return cleaned

    @field_validator("terminal")
    @classmethod
    def _terminal_valid(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() if isinstance(s, str) else s for s in v]
        if any(not s for s in cleaned):
            raise ValueError("terminal stage names must be non-empty")
        dupes = _duplicates(cleaned)
        if dupes:
            raise ValueError(f"duplicate terminal stage(s): {', '.join(dupes)}")
        return cleaned

    @model_validator(mode="after")
    def _terminal_disjoint_from_stages(self) -> "PipelineSpec":
        overlap = sorted(set(self.stages) & set(self.terminal))
        if overlap:
            raise ValueError(
                f"terminal stage(s) also listed as pipeline stage(s): {', '.join(overlap)}"
            )
        return self


class TaskStateSchema(BaseModel):
    """The full `tasks.state_schema` contract for one task."""

    version: int = 1
    entity: EntitySpec | None = None  # None -> singleton task (SINGLETON_KEY)
    pipeline: PipelineSpec

    @field_validator("version")
    @classmethod
    def _version_is_1(cls, v: int) -> int:
        if v != 1:
            raise ValueError(f"unsupported schema version {v} (only version 1 exists)")
        return v

    def all_stages(self) -> list[str]:
        """Non-terminal stages followed by terminal stages, in schema order."""
        return [*self.pipeline.stages, *self.pipeline.terminal]

    def attr(self, key: str) -> AttributeSpec | None:
        """Look up an entity attribute by key; None if absent or singleton task."""
        if self.entity is None:
            return None
        for a in self.entity.attributes:
            if a.key == key:
                return a
        return None


def _format_validation_error(exc: ValidationError) -> str:
    """Compact, human-readable rendering of a pydantic ValidationError.

    Fed back to the LLM on retry in the propose flow, so it needs to read as
    plain English, not a stack of pydantic internals.
    """
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        msg = err["msg"]
        # pydantic prefixes model_validator/field_validator ValueError messages
        # with "Value error, " — internal plumbing noise, not useful context
        # for the LLM retry loop this message is fed back to.
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, ") :]
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def validate_schema(raw: dict) -> TaskStateSchema:
    """Pydantic parse + the cross-field rules encoded on the models above.

    Raises ValueError with a human-readable message (never pydantic's
    ValidationError) so callers — notably the LLM propose/retry loop — can
    catch one exception type and feed the message straight back as context.
    """
    try:
        return TaskStateSchema.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(_format_validation_error(exc)) from exc


def _parse_datetime(value: str) -> datetime:
    v = value.strip()
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        pass
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    raise ValueError(f"'{value}' is not a recognizable datetime")


def coerce_value(spec_type: str, value: str, *, enum_values: list[str] | None = None) -> str:
    """Validate + normalize a raw string for an attribute type.

    Returns the normalized string that gets stored in state / written to
    `task_events.new_value`:
    - datetime -> ISO-8601 (raises ValueError if unparseable)
    - number   -> canonical str (int-valued floats drop the trailing '.0')
    - boolean  -> 'true'/'false' (accepts true/false/yes/no, case-insensitive)
    - enum     -> exact member (raises if not in enum_values)
    - string   -> stripped
    """
    if spec_type not in ATTR_TYPES:
        raise ValueError(f"unknown attribute type '{spec_type}'")

    if spec_type == "string":
        return value.strip()

    if spec_type == "number":
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"'{value}' is not a valid number") from None
        if not math.isfinite(f):
            raise ValueError(f"'{value}' is not a finite number")
        return str(int(f)) if f.is_integer() else str(f)

    if spec_type == "boolean":
        v = value.strip().lower()
        if v in ("true", "yes"):
            return "true"
        if v in ("false", "no"):
            return "false"
        raise ValueError(f"'{value}' is not a valid boolean (expected true/false/yes/no)")

    if spec_type == "datetime":
        return _parse_datetime(value).isoformat()

    # spec_type == "enum"
    if not enum_values:
        raise ValueError("enum coercion requires a non-empty enum_values list")
    v = value.strip()
    if v not in enum_values:
        raise ValueError(f"'{value}' is not a valid enum member; expected one of {enum_values}")
    return v


def assert_additive_change(old: TaskStateSchema, new: TaskStateSchema) -> None:
    """Enforce the additive-only edit rule used by PATCH.

    Allowed: new stages/terminal entries appended anywhere, new attributes,
    new enum members appended to an existing enum attribute, entity
    noun/identity_hint edits.
    Rejected (ValueError naming what changed): removed stages, removed
    terminal entries, removed attributes, attribute type changes (a type
    change is a remove+add of the same key, which v1 does not support —
    renames aren't supported either), removed enum member(s) on an
    otherwise-unchanged enum attribute, and singleton<->entity flips.
    """
    if (old.entity is None) != (new.entity is None):
        raise ValueError(
            "cannot change a singleton task to an entity-based task (or vice versa)"
        )

    removed_stages = sorted(set(old.pipeline.stages) - set(new.pipeline.stages))
    if removed_stages:
        raise ValueError(f"cannot remove pipeline stage(s): {', '.join(removed_stages)}")

    removed_terminal = sorted(set(old.pipeline.terminal) - set(new.pipeline.terminal))
    if removed_terminal:
        raise ValueError(f"cannot remove terminal stage(s): {', '.join(removed_terminal)}")

    if old.entity is not None and new.entity is not None:
        old_attrs = {a.key: a for a in old.entity.attributes}
        new_attrs = {a.key: a for a in new.entity.attributes}

        removed_attrs = sorted(set(old_attrs) - set(new_attrs))
        if removed_attrs:
            raise ValueError(f"cannot remove attribute(s): {', '.join(removed_attrs)}")

        for key, old_attr in old_attrs.items():
            new_attr = new_attrs[key]
            if new_attr.type != old_attr.type:
                raise ValueError(
                    f"cannot change type of attribute '{key}' from '{old_attr.type}' to "
                    f"'{new_attr.type}' (a type change is remove+add, which v1 rejects)"
                )
            if old_attr.type == "enum" and new_attr.type == "enum":
                removed_members = sorted(
                    set(old_attr.values or []) - set(new_attr.values or [])
                )
                if removed_members:
                    raise ValueError(
                        f"cannot remove enum member(s) from attribute '{key}': "
                        f"{', '.join(removed_members)}"
                    )
