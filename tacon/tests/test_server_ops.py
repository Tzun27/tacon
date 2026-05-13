"""Unit tests for ``tacon.server_ops.build_op`` — the request-args →
Op-instance bridge used by the v0.3 GUI server.

These tests cover the per-op translation logic (callable construction
for FixCIWorkflow, rule-dataclass building for AddBranchProtection)
in isolation from FastAPI / SQLite / GitHub, so failures here
pinpoint the bridge layer rather than the surrounding HTTP plumbing.
"""

from __future__ import annotations

import pytest

from tacon.ops.add_branch_protection import AddBranchProtection
from tacon.ops.add_ci_workflow import AddCIWorkflow
from tacon.ops.add_file import AddFile
from tacon.ops.delete_file import DeleteFile
from tacon.ops.fix_ci_workflow import FixCIWorkflow
from tacon.server_ops import OpBuildError, build_op

_MIN_WORKFLOW = """name: x
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""


# ---------- add-file ----------


def test_build_op_add_file_passes_through() -> None:
    op = build_op(
        "add-file",
        {"path": "STARTER.md", "content": "hello\n"},
    )
    assert isinstance(op, AddFile)
    assert op.path == "STARTER.md"
    assert op.content == "hello\n"
    assert op.via_pr is False  # default flows through


def test_build_op_add_file_respects_via_pr_and_message() -> None:
    op = build_op(
        "add-file",
        {
            "path": "X",
            "content": "y",
            "message": "custom msg",
            "assignment_id": "asn-1",
            "via_pr": True,
        },
    )
    assert isinstance(op, AddFile)
    assert op.message == "custom msg"
    assert op.assignment_id == "asn-1"
    assert op.via_pr is True


# ---------- delete-file ----------


def test_build_op_delete_file_passes_through() -> None:
    op = build_op("delete-file", {"path": "OBSOLETE.md"})
    assert isinstance(op, DeleteFile)
    assert op.path == "OBSOLETE.md"


# ---------- add-ci-workflow ----------


def test_build_op_add_ci_workflow_with_valid_yaml() -> None:
    op = build_op(
        "add-ci-workflow",
        {"name": "ci", "content": _MIN_WORKFLOW},
    )
    assert isinstance(op, AddCIWorkflow)
    assert op.workflow_name == "ci"


def test_build_op_add_ci_workflow_rejects_bad_yaml() -> None:
    """WorkflowValidationError surfaces as OpBuildError so the API can
    return 422 instead of an unhandled 500."""
    with pytest.raises(OpBuildError, match="add-ci-workflow"):
        build_op(
            "add-ci-workflow",
            {"name": "ci", "content": "not: { valid: yaml :: ::"},
        )


# ---------- fix-ci-workflow ----------


def test_build_op_fix_ci_workflow_builds_transform_callable() -> None:
    op = build_op(
        "fix-ci-workflow",
        {
            "name": "ci",
            "bump_action_from": "actions/checkout@v3",
            "bump_action_to": "actions/checkout@v4",
        },
    )
    assert isinstance(op, FixCIWorkflow)
    # transform_id round-trip — used by `tacon resume` and visible in the
    # events table, so locked here.
    assert op.transform_id == "bump-action actions/checkout@v3->actions/checkout@v4"
    # Transform itself: a callable that maps v3→v4 in real workflow text.
    rewritten = op.transform(b"uses: actions/checkout@v3\n")
    assert rewritten == b"uses: actions/checkout@v4\n"


def test_build_op_fix_ci_workflow_rejects_bad_bump_ref() -> None:
    with pytest.raises(OpBuildError, match="fix-ci-workflow"):
        build_op(
            "fix-ci-workflow",
            {
                "name": "ci",
                "bump_action_from": "",  # empty ref — make_bump_action_transform rejects
                "bump_action_to": "actions/checkout@v4",
            },
        )


# ---------- add-branch-protection ----------


def test_build_op_add_branch_protection_survey_mode_when_rule_missing() -> None:
    op = build_op("add-branch-protection", {})
    assert isinstance(op, AddBranchProtection)
    assert op.rule is None  # survey mode


def test_build_op_add_branch_protection_survey_mode_when_rule_null() -> None:
    op = build_op("add-branch-protection", {"rule": None})
    assert isinstance(op, AddBranchProtection)
    assert op.rule is None


def test_build_op_add_branch_protection_write_mode_with_rule_dict() -> None:
    op = build_op(
        "add-branch-protection",
        {
            "branch": "main",
            "rule": {
                "required_approving_review_count": 1,
                "dismiss_stale_reviews": True,
            },
        },
    )
    assert isinstance(op, AddBranchProtection)
    assert op.rule is not None
    assert op.rule.required_approving_review_count == 1
    assert op.rule.dismiss_stale_reviews is True
    assert op.branch == "main"


def test_build_op_add_branch_protection_rejects_unknown_rule_key() -> None:
    """Strict validation in BranchProtectionRule.from_dict surfaces as
    OpBuildError so a typo doesn't silently no-op."""
    with pytest.raises(OpBuildError, match="rule"):
        build_op(
            "add-branch-protection",
            {"rule": {"requiered_status_checks": ["ci"]}},  # typo
        )


# ---------- unknown op ----------


def test_build_op_unknown_op_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="not-a-real-op"):
        build_op("not-a-real-op", {})
