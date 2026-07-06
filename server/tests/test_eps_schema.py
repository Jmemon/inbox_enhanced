"""Tests for the EPS (Entity-Pipeline Schema) language.

Pure pydantic module — no database, no app state. These tests exercise the
contract that the extraction validator (Task 6) and the future board UI will
rely on mechanically: parsing/validating raw dicts into TaskStateSchema,
coercing raw string values per attribute type, and the additive-only edit
rule enforced on PATCH.
"""

import pytest

from app.task_engine.schema import (
    ATTR_TYPES,
    RESERVED_FIELD,
    SINGLETON_KEY,
    AttributeSpec,
    EntitySpec,
    PipelineSpec,
    TaskStateSchema,
    assert_additive_change,
    coerce_value,
    validate_schema,
)


def job_hunt_raw() -> dict:
    """A realistic multi-entity job-hunt schema."""
    return {
        "version": 1,
        "entity": {
            "noun": "company",
            "identity_hint": "the hiring company name",
            "attributes": [
                {"key": "recruiter", "type": "string"},
                {"key": "next_step_date", "type": "datetime"},
                {"key": "referral", "type": "boolean"},
                {"key": "level", "type": "enum", "values": ["junior", "mid", "senior"]},
            ],
        },
        "pipeline": {
            "stages": ["applied", "phone screen", "onsite"],
            "terminal": ["offer", "rejected", "withdrawn"],
        },
    }


def singleton_raw() -> dict:
    """A singleton task schema — no entity, e.g. 'track my visa application'."""
    return {
        "version": 1,
        "entity": None,
        "pipeline": {
            "stages": ["submitted", "in review"],
            "terminal": ["approved", "denied"],
        },
    }


# --- constants -------------------------------------------------------------


def test_constants():
    assert ATTR_TYPES == {"string", "number", "datetime", "boolean", "enum"}
    assert RESERVED_FIELD == "stage"
    assert SINGLETON_KEY == "_self"


# --- validate_schema: happy paths ------------------------------------------


def test_valid_job_hunt_schema_parses():
    schema = validate_schema(job_hunt_raw())
    assert isinstance(schema, TaskStateSchema)
    assert schema.version == 1
    assert schema.entity is not None
    assert schema.entity.noun == "company"
    assert len(schema.entity.attributes) == 4
    assert schema.pipeline.stages == ["applied", "phone screen", "onsite"]
    assert schema.pipeline.terminal == ["offer", "rejected", "withdrawn"]


def test_singleton_schema_parses():
    schema = validate_schema(singleton_raw())
    assert schema.entity is None
    assert schema.pipeline.stages == ["submitted", "in review"]


def test_entity_defaults():
    # attributes/identity_hint are optional
    spec = EntitySpec(noun="apartment")
    assert spec.identity_hint == ""
    assert spec.attributes == []


def test_pipeline_terminal_defaults_empty():
    spec = PipelineSpec(stages=["a"])
    assert spec.terminal == []


# --- validate_schema: cross-field rejections --------------------------------


def test_duplicate_stage_rejected():
    raw = singleton_raw()
    raw["pipeline"]["stages"] = ["submitted", "in review", "submitted"]
    with pytest.raises(ValueError, match="submitted"):
        validate_schema(raw)


def test_duplicate_terminal_rejected():
    raw = singleton_raw()
    raw["pipeline"]["terminal"] = ["approved", "approved"]
    with pytest.raises(ValueError, match="approved"):
        validate_schema(raw)


def test_terminal_overlaps_stages_rejected():
    raw = singleton_raw()
    raw["pipeline"]["terminal"] = ["submitted"]  # already a stage
    with pytest.raises(ValueError, match="submitted"):
        validate_schema(raw)


def test_empty_stages_rejected():
    raw = singleton_raw()
    raw["pipeline"]["stages"] = []
    with pytest.raises(ValueError):
        validate_schema(raw)


def test_blank_stage_name_rejected():
    raw = singleton_raw()
    raw["pipeline"]["stages"] = ["submitted", "   "]
    with pytest.raises(ValueError):
        validate_schema(raw)


def test_blank_terminal_name_rejected():
    raw = singleton_raw()
    raw["pipeline"]["terminal"] = ["approved", "   "]
    with pytest.raises(ValueError):
        validate_schema(raw)


def test_attr_key_reserved_field_rejected():
    raw = job_hunt_raw()
    raw["entity"]["attributes"].append({"key": "stage", "type": "string"})
    with pytest.raises(ValueError, match="stage"):
        validate_schema(raw)


def test_attr_key_empty_rejected():
    raw = job_hunt_raw()
    raw["entity"]["attributes"].append({"key": "  ", "type": "string"})
    with pytest.raises(ValueError):
        validate_schema(raw)


def test_duplicate_attr_key_rejected():
    raw = job_hunt_raw()
    raw["entity"]["attributes"].append({"key": "recruiter", "type": "string"})
    with pytest.raises(ValueError, match="recruiter"):
        validate_schema(raw)


def test_enum_without_values_rejected():
    raw = job_hunt_raw()
    raw["entity"]["attributes"].append({"key": "priority", "type": "enum"})
    with pytest.raises(ValueError, match="priority"):
        validate_schema(raw)


def test_enum_with_empty_values_list_rejected():
    raw = job_hunt_raw()
    raw["entity"]["attributes"].append(
        {"key": "priority", "type": "enum", "values": []}
    )
    with pytest.raises(ValueError, match="priority"):
        validate_schema(raw)


def test_non_enum_with_values_rejected():
    raw = job_hunt_raw()
    raw["entity"]["attributes"].append(
        {"key": "note", "type": "string", "values": ["a", "b"]}
    )
    with pytest.raises(ValueError, match="note"):
        validate_schema(raw)


def test_unknown_attr_type_rejected():
    raw = job_hunt_raw()
    raw["entity"]["attributes"].append({"key": "weird", "type": "regex"})
    with pytest.raises(ValueError, match="regex"):
        validate_schema(raw)


def test_version_must_be_1():
    raw = singleton_raw()
    raw["version"] = 2
    with pytest.raises(ValueError, match="version"):
        validate_schema(raw)


def test_entity_noun_empty_rejected():
    raw = job_hunt_raw()
    raw["entity"]["noun"] = ""
    with pytest.raises(ValueError):
        validate_schema(raw)


def test_validate_schema_error_is_value_error_not_pydantic():
    # Ensure pydantic's ValidationError is caught and translated — this
    # matters because the message is fed back to an LLM on retry, and the
    # caller (propose flow) only ever catches ValueError.
    raw = singleton_raw()
    raw["pipeline"]["stages"] = []
    try:
        validate_schema(raw)
        pytest.fail("expected ValueError")
    except ValueError as e:
        assert type(e) is ValueError
        assert str(e)  # non-empty human-readable message


def test_validate_schema_error_strips_pydantic_value_error_prefix():
    # model_validator(mode="after") raises land in pydantic as "Value error, <msg>";
    # that internal prefix is noise for an LLM reading the retry message, so
    # _format_validation_error must strip it before it reaches validate_schema's
    # caller.
    raw = singleton_raw()
    raw["pipeline"]["terminal"] = ["submitted"]  # overlaps a stage -> model_validator raises
    try:
        validate_schema(raw)
        pytest.fail("expected ValueError")
    except ValueError as e:
        assert "Value error" not in str(e)


def test_validate_schema_garbage_input_rejected():
    with pytest.raises(ValueError):
        validate_schema({"version": 1, "pipeline": "not-a-dict"})


# --- TaskStateSchema helper methods -----------------------------------------


def test_all_stages_combines_stages_and_terminal():
    schema = validate_schema(job_hunt_raw())
    assert schema.all_stages() == [
        "applied",
        "phone screen",
        "onsite",
        "offer",
        "rejected",
        "withdrawn",
    ]


def test_attr_lookup_hit_and_miss():
    schema = validate_schema(job_hunt_raw())
    found = schema.attr("recruiter")
    assert isinstance(found, AttributeSpec)
    assert found.type == "string"
    assert schema.attr("does_not_exist") is None


def test_attr_lookup_on_singleton_returns_none():
    schema = validate_schema(singleton_raw())
    assert schema.attr("anything") is None


# --- coerce_value: string ----------------------------------------------------


def test_coerce_string_strips_whitespace():
    assert coerce_value("string", "  hello world  ") == "hello world"


# --- coerce_value: number -----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("42", "42"),
        ("42.0", "42"),
        ("3.14", "3.14"),
        ("-7", "-7"),
        ("  10  ", "10"),
    ],
)
def test_coerce_number_happy(raw, expected):
    assert coerce_value("number", raw) == expected


def test_coerce_number_rejects_non_numeric():
    with pytest.raises(ValueError):
        coerce_value("number", "not-a-number")


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
def test_coerce_number_rejects_non_finite(raw):
    with pytest.raises(ValueError):
        coerce_value("number", raw)


# --- coerce_value: boolean -----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", "true"),
        ("True", "true"),
        ("TRUE", "true"),
        ("yes", "true"),
        ("Yes", "true"),
        ("false", "false"),
        ("False", "false"),
        ("no", "false"),
        ("No", "false"),
    ],
)
def test_coerce_boolean_happy(raw, expected):
    assert coerce_value("boolean", raw) == expected


def test_coerce_boolean_rejects_garbage():
    with pytest.raises(ValueError):
        coerce_value("boolean", "maybe")


# --- coerce_value: datetime -----------------------------------------------------


def test_coerce_datetime_iso8601_roundtrips():
    result = coerce_value("datetime", "2026-07-06T10:30:00")
    assert result.startswith("2026-07-06T10:30:00")


@pytest.mark.parametrize(
    "raw",
    [
        "2026-07-06",
        "2026-07-06 10:30",
        "2026-07-06 10:30:00",
        "07/06/2026",
    ],
)
def test_coerce_datetime_forgiving_formats(raw):
    result = coerce_value("datetime", raw)
    assert result.startswith("2026-07-06")


def test_coerce_datetime_rejects_unparseable():
    with pytest.raises(ValueError):
        coerce_value("datetime", "not a date at all")


# --- coerce_value: enum -----------------------------------------------------


def test_coerce_enum_happy():
    result = coerce_value("enum", "mid", enum_values=["junior", "mid", "senior"])
    assert result == "mid"


def test_coerce_enum_rejects_non_member():
    with pytest.raises(ValueError):
        coerce_value("enum", "staff", enum_values=["junior", "mid", "senior"])


def test_coerce_enum_without_enum_values_raises():
    with pytest.raises(ValueError):
        coerce_value("enum", "mid")


# --- coerce_value: unknown type -----------------------------------------------------


def test_coerce_value_unknown_type_rejected():
    with pytest.raises(ValueError):
        coerce_value("regex", "abc")


# --- assert_additive_change: accepted (additive) changes --------------------


def test_additive_change_accepts_added_stage():
    old = validate_schema(job_hunt_raw())
    raw = job_hunt_raw()
    raw["pipeline"]["stages"] = ["applied", "referred", "phone screen", "onsite"]
    new = validate_schema(raw)
    assert_additive_change(old, new)  # should not raise


def test_additive_change_accepts_added_terminal():
    old = validate_schema(job_hunt_raw())
    raw = job_hunt_raw()
    raw["pipeline"]["terminal"].append("ghosted")
    new = validate_schema(raw)
    assert_additive_change(old, new)  # should not raise


def test_additive_change_accepts_added_attribute():
    old = validate_schema(job_hunt_raw())
    raw = job_hunt_raw()
    raw["entity"]["attributes"].append({"key": "salary_range", "type": "string"})
    new = validate_schema(raw)
    assert_additive_change(old, new)  # should not raise


def test_additive_change_accepts_entity_noun_edit():
    old = validate_schema(job_hunt_raw())
    raw = job_hunt_raw()
    raw["entity"]["noun"] = "employer"
    raw["entity"]["identity_hint"] = "updated hint"
    new = validate_schema(raw)
    assert_additive_change(old, new)  # should not raise


def test_additive_change_noop_is_fine():
    old = validate_schema(job_hunt_raw())
    new = validate_schema(job_hunt_raw())
    assert_additive_change(old, new)  # should not raise


# --- assert_additive_change: rejected (destructive) changes -----------------


def test_additive_change_rejects_removed_stage():
    old = validate_schema(job_hunt_raw())
    raw = job_hunt_raw()
    raw["pipeline"]["stages"] = ["applied", "onsite"]  # dropped "phone screen"
    new = validate_schema(raw)
    with pytest.raises(ValueError, match="phone screen"):
        assert_additive_change(old, new)


def test_additive_change_rejects_removed_terminal():
    old = validate_schema(job_hunt_raw())
    raw = job_hunt_raw()
    raw["pipeline"]["terminal"] = ["offer", "rejected"]  # dropped "withdrawn"
    new = validate_schema(raw)
    with pytest.raises(ValueError, match="withdrawn"):
        assert_additive_change(old, new)


def test_additive_change_rejects_removed_attribute():
    old = validate_schema(job_hunt_raw())
    raw = job_hunt_raw()
    raw["entity"]["attributes"] = [
        a for a in raw["entity"]["attributes"] if a["key"] != "referral"
    ]
    new = validate_schema(raw)
    with pytest.raises(ValueError, match="referral"):
        assert_additive_change(old, new)


def test_additive_change_rejects_attribute_type_change():
    old = validate_schema(job_hunt_raw())
    raw = job_hunt_raw()
    for a in raw["entity"]["attributes"]:
        if a["key"] == "recruiter":
            a["type"] = "number"
    new = validate_schema(raw)
    with pytest.raises(ValueError, match="recruiter"):
        assert_additive_change(old, new)


def test_additive_change_rejects_enum_member_removal():
    old = validate_schema(job_hunt_raw())
    raw = job_hunt_raw()
    for a in raw["entity"]["attributes"]:
        if a["key"] == "level":
            a["values"] = ["junior", "senior"]  # dropped "mid"
    new = validate_schema(raw)
    with pytest.raises(ValueError, match="mid"):
        assert_additive_change(old, new)


def test_additive_change_accepts_enum_member_addition():
    old = validate_schema(job_hunt_raw())
    raw = job_hunt_raw()
    for a in raw["entity"]["attributes"]:
        if a["key"] == "level":
            a["values"] = ["junior", "mid", "senior", "staff"]  # widened
    new = validate_schema(raw)
    assert_additive_change(old, new)  # should not raise


def test_additive_change_rejects_singleton_to_entity_flip():
    old = validate_schema(singleton_raw())
    raw = job_hunt_raw()
    new = validate_schema(raw)
    with pytest.raises(ValueError):
        assert_additive_change(old, new)


def test_additive_change_rejects_entity_to_singleton_flip():
    old = validate_schema(job_hunt_raw())
    new = validate_schema(singleton_raw())
    with pytest.raises(ValueError):
        assert_additive_change(old, new)
