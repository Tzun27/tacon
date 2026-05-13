"""Server-side op construction.

Bridges the gap between the JSON request body (validated against an op's
``arg_schema()`` Pydantic model) and the op's ``__init__`` kwargs.

Some ops have a direct mapping (AddFile, DeleteFile, AddCIWorkflow): the
form fields ARE the constructor kwargs. Two ops need translation:

- ``FixCIWorkflow`` exposes ``bump_action_from`` / ``bump_action_to`` to
  the form but the constructor takes a ``transform`` callable. We build
  the callable via ``make_bump_action_transform`` and synthesize a
  ``transform_id`` string in the same shape the CLI uses.
- ``AddBranchProtection`` accepts a nested ``rule`` object (or None for
  survey mode). The ``BranchProtectionRule`` dataclass + its
  ``from_dict`` validator do the heavy lifting.

Lives in its own module (rather than ``server.py``) so it can be imported
without pulling in FastAPI / uvicorn — keeps the dependency graph small
for tests that only need to validate construction logic.
"""

from __future__ import annotations

from typing import Any

from tacon.ops import Op, get_op_class
from tacon.ops._branch_protection_rule import (
    BranchProtectionRule,
    RuleValidationError,
)
from tacon.ops._branch_protection_rule import from_dict as _rule_from_dict
from tacon.ops.add_branch_protection import AddBranchProtection
from tacon.ops.add_ci_workflow import AddCIWorkflow, WorkflowValidationError
from tacon.ops.add_file import AddFile
from tacon.ops.delete_file import DeleteFile
from tacon.ops.fix_ci_workflow import FixCIWorkflow, make_bump_action_transform


class OpBuildError(ValueError):
    """Raised when a request body is well-typed but semantically invalid.

    Examples: invalid bump-action ref, malformed protection-rule dict,
    workflow YAML that fails validation. The server maps this to HTTP 422
    so the GUI can surface the message in-line on the form.
    """


def build_op(name: str, validated_args: dict[str, Any]) -> Op:
    """Construct an Op instance from a validated args dict.

    ``validated_args`` is a plain dict from a Pydantic model that has
    already passed ``arg_schema().model_validate(...)``. Field-shape
    validation is the caller's responsibility; this function handles
    only the per-op semantic translation (callable construction, nested
    dataclass building, etc.).

    Raises :class:`OpBuildError` for op-specific validation failures
    (e.g. workflow YAML, bump-action format). Raises ``KeyError`` for
    unknown op names — get_op_class() handles that.
    """
    op_cls = get_op_class(name)

    if op_cls is AddFile:
        return AddFile(
            path=validated_args["path"],
            content=validated_args["content"],
            message=validated_args.get("message", "tacon: add file"),
            assignment_id=validated_args.get("assignment_id"),
            via_pr=validated_args.get("via_pr", False),
        )

    if op_cls is DeleteFile:
        return DeleteFile(
            path=validated_args["path"],
            message=validated_args.get("message", "tacon: delete file"),
            assignment_id=validated_args.get("assignment_id"),
            via_pr=validated_args.get("via_pr", False),
        )

    if op_cls is AddCIWorkflow:
        try:
            return AddCIWorkflow(
                name=validated_args["name"],
                content=validated_args["content"],
                message=validated_args.get("message"),
                assignment_id=validated_args.get("assignment_id"),
                via_pr=validated_args.get("via_pr", False),
            )
        except WorkflowValidationError as exc:
            raise OpBuildError(f"add-ci-workflow: {exc}") from exc

    if op_cls is FixCIWorkflow:
        from_ref = validated_args["bump_action_from"]
        to_ref = validated_args["bump_action_to"]
        try:
            transform = make_bump_action_transform(from_ref, to_ref)
        except ValueError as exc:
            raise OpBuildError(f"fix-ci-workflow: {exc}") from exc
        try:
            return FixCIWorkflow(
                name=validated_args["name"],
                transform=transform,
                transform_id=f"bump-action {from_ref}->{to_ref}",
                message=validated_args.get("message", "tacon: fix CI workflow"),
                assignment_id=validated_args.get("assignment_id"),
                via_pr=validated_args.get("via_pr", False),
            )
        except WorkflowValidationError as exc:
            raise OpBuildError(f"fix-ci-workflow: {exc}") from exc

    if op_cls is AddBranchProtection:
        rule_dict = validated_args.get("rule")
        rule: BranchProtectionRule | None = None
        if isinstance(rule_dict, dict):
            try:
                rule = _rule_from_dict(rule_dict)
            except RuleValidationError as exc:
                raise OpBuildError(f"add-branch-protection: rule: {exc}") from exc
        return AddBranchProtection(
            branch=validated_args.get("branch"),
            assignment_id=validated_args.get("assignment_id"),
            rule=rule,
        )

    # Defensive: any future op that lacks a build_op branch should error
    # loudly instead of silently constructing with the wrong signature.
    raise OpBuildError(
        f"build_op: no constructor mapping for op {name!r} ({op_cls.__name__}). "
        "Add a branch to tacon.server_ops.build_op."
    )
