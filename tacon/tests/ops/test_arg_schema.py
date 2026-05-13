"""Tests for ``Op.arg_schema()`` — the v0.3 GUI's form-generation contract.

Each Op subclass returns a Pydantic ``BaseModel`` whose fields mirror its
constructor kwargs. The GUI calls ``arg_schema().model_json_schema()`` to
render forms; FastAPI uses the same model as a request body for free
validation. Tests assert: schemas exist, are pydantic, JSON-roundtrip,
and key fields land in the expected types.
"""

from __future__ import annotations

from pydantic import BaseModel

from tacon.ops import Op, get_op_class, list_ops


def test_op_abc_default_returns_empty_schema() -> None:
    """The Op ABC's default ``arg_schema`` returns an empty BaseModel.
    Ops without configurable args inherit this; the GUI shows a
    parameter-less form."""

    class Bare(Op):
        def plan(self, db, gh):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        def apply(self, db, gh, diff, confirm):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    schema = Bare.arg_schema()
    assert issubclass(schema, BaseModel)
    assert schema.model_json_schema()["properties"] == {}


def test_every_registered_op_has_a_pydantic_arg_schema() -> None:
    """Every Op in the registry returns a Pydantic BaseModel from
    arg_schema(). Without this, the GUI can't auto-generate forms for it."""
    for name in list_ops():
        cls = get_op_class(name)
        schema = cls.arg_schema()
        assert issubclass(schema, BaseModel), (
            f"{name}.arg_schema() must return a pydantic.BaseModel subclass"
        )


def test_every_arg_schema_serializes_to_json_schema() -> None:
    """The GUI consumes JSON Schema (via .model_json_schema()); this is the
    runtime contract for form generation."""
    for name in list_ops():
        schema_cls = get_op_class(name).arg_schema()
        json_schema = schema_cls.model_json_schema()
        # JSON Schema must be a dict with at minimum 'properties' OR
        # 'type' (empty schemas have neither at the top level but pydantic
        # writes 'properties': {} regardless).
        assert isinstance(json_schema, dict)


def test_add_file_arg_schema_shape() -> None:
    schema = get_op_class("add-file").arg_schema()
    fields = schema.model_fields
    assert "path" in fields
    assert "content" in fields
    assert "message" in fields
    assert "assignment_id" in fields
    assert "via_pr" in fields
    # path and content are required (no default)
    assert fields["path"].is_required()
    assert fields["content"].is_required()
    # via_pr defaults False
    assert fields["via_pr"].default is False
    # message has its CLI default
    assert fields["message"].default == "tacon: add file"


def test_add_file_arg_schema_validates_required_fields() -> None:
    """Missing required fields raise pydantic ValidationError. The
    FastAPI plan/apply endpoints will surface this as a 422 with a
    field-level error message."""
    import pytest
    from pydantic import ValidationError

    schema = get_op_class("add-file").arg_schema()
    with pytest.raises(ValidationError):
        schema()  # missing path, content
    # With valid fields, it constructs cleanly.
    instance = schema(path="STARTER.md", content="hello")
    assert instance.path == "STARTER.md"
    assert instance.via_pr is False


def test_delete_file_arg_schema_shape() -> None:
    schema = get_op_class("delete-file").arg_schema()
    fields = schema.model_fields
    assert fields["path"].is_required()
    assert "content" not in fields  # delete doesn't need content
    assert fields["message"].default == "tacon: delete file"


def test_add_ci_workflow_arg_schema_shape() -> None:
    schema = get_op_class("add-ci-workflow").arg_schema()
    fields = schema.model_fields
    assert "name" in fields  # workflow name, not path
    assert "content" in fields  # the YAML body
    assert fields["name"].is_required()
    assert fields["content"].is_required()
    # message is optional (defaults None → CLI-side default)
    assert fields["message"].default is None


def test_fix_ci_workflow_arg_schema_surfaces_bump_action_pair() -> None:
    """FixCIWorkflow's runtime ``__init__`` takes a Callable transform that
    can't be serialized as a form input. The schema exposes the
    bump_action_from/bump_action_to pair instead; the server builds the
    transform via make_bump_action_transform before instantiation."""
    schema = get_op_class("fix-ci-workflow").arg_schema()
    fields = schema.model_fields
    assert "name" in fields
    assert "bump_action_from" in fields
    assert "bump_action_to" in fields
    # transform / transform_id never appear in the schema — those are
    # constructed server-side from bump_action_from/to.
    assert "transform" not in fields
    assert "transform_id" not in fields


def test_add_branch_protection_arg_schema_has_optional_rule_subschema() -> None:
    """AddBranchProtection's schema embeds the rule shape as a nested
    optional model. None → survey mode; populated → write mode."""
    schema = get_op_class("add-branch-protection").arg_schema()
    fields = schema.model_fields
    assert "branch" in fields
    assert "rule" in fields
    # rule is optional (survey mode = rule=None)
    assert fields["rule"].default is None
    # JSON-schema dump must contain the nested rule definition
    json_schema = schema.model_json_schema()
    # Pydantic 2 stores nested models in $defs
    defs = json_schema.get("$defs", {})
    assert any(
        "BranchProtectionRule" in name for name in defs
    ), f"expected a BranchProtectionRule schema in $defs, got: {list(defs.keys())}"


def test_add_branch_protection_arg_schema_clamps_review_count() -> None:
    """GitHub allows 1-6 required reviews; pydantic enforces the range."""
    import pytest
    from pydantic import ValidationError

    schema = get_op_class("add-branch-protection").arg_schema()
    rule_cls = schema.model_fields["rule"].annotation
    # rule_cls is `BranchProtectionRuleArgs | None`; pull out the model
    # Pydantic exposes it as the first arg of the Union
    import typing as t

    rule_model = next(
        arg for arg in t.get_args(rule_cls) if arg is not type(None)  # noqa: E721
    )
    # In-range: ok
    rule_model(required_approving_review_count=3)
    # Out-of-range: rejected
    with pytest.raises(ValidationError):
        rule_model(required_approving_review_count=7)
