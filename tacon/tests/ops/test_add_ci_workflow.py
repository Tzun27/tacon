"""Tests for AddCIWorkflow: validation + plan/apply/rollback inheritance."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from github import UnknownObjectException
from sqlite_utils import Database

from tacon.db import get_events_by_op
from tacon.ops import get_op_class
from tacon.ops.add_ci_workflow import AddCIWorkflow, WorkflowValidationError

VALID_WORKFLOW = """\
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pytest
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: ruff check .
"""


def _content_file(sha: str = "blob-sha-1") -> MagicMock:
    cf = MagicMock(name="ContentFile")
    cf.sha = sha
    return cf


def _commit(sha: str = "commit-sha-1") -> MagicMock:
    c = MagicMock(name="Commit")
    c.sha = sha
    return c


def _missing() -> UnknownObjectException:
    return UnknownObjectException(404, {"message": "Not Found"}, {})


# ---------- registry ----------


def test_add_ci_workflow_is_registered() -> None:
    assert get_op_class("add-ci-workflow") is AddCIWorkflow


# ---------- validation ----------


def test_init_rejects_unsafe_name() -> None:
    with pytest.raises(WorkflowValidationError, match="invalid workflow name"):
        AddCIWorkflow(name="../../etc/passwd", content=VALID_WORKFLOW)


def test_init_rejects_empty_name() -> None:
    with pytest.raises(WorkflowValidationError, match="invalid workflow name"):
        AddCIWorkflow(name="", content=VALID_WORKFLOW)


def test_init_rejects_unparseable_yaml() -> None:
    with pytest.raises(WorkflowValidationError, match="did not parse"):
        AddCIWorkflow(name="ci", content="key: [unbalanced")


def test_init_rejects_yaml_without_jobs() -> None:
    with pytest.raises(WorkflowValidationError, match="jobs"):
        AddCIWorkflow(name="ci", content="on: [push]\n")


def test_init_rejects_yaml_without_on_block() -> None:
    with pytest.raises(WorkflowValidationError, match="on"):
        AddCIWorkflow(name="ci", content="jobs:\n  t:\n    runs-on: ubuntu-latest\n")


def test_init_rejects_top_level_list() -> None:
    with pytest.raises(WorkflowValidationError, match="mapping"):
        AddCIWorkflow(name="ci", content="- a\n- b\n")


def test_init_accepts_explicit_yaml_extension() -> None:
    op = AddCIWorkflow(name="ci.yaml", content=VALID_WORKFLOW)
    assert op.path == ".github/workflows/ci.yaml"


def test_init_appends_yml_extension_by_default() -> None:
    op = AddCIWorkflow(name="ci", content=VALID_WORKFLOW)
    assert op.path == ".github/workflows/ci.yml"


def test_init_counts_jobs() -> None:
    op = AddCIWorkflow(name="ci", content=VALID_WORKFLOW)
    assert op.job_count == 2


# ---------- plan ----------


def test_plan_summary_is_workflow_aware(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.side_effect = _missing()
    op = AddCIWorkflow(name="ci", content=VALID_WORKFLOW)
    diff = op.plan(tmp_db, fake_gh)

    assert len(diff.per_repo) == 3
    assert all("'ci'" in r.summary for r in diff.per_repo)
    assert all("(2 jobs)" in r.summary for r in diff.per_repo)


def test_plan_blocked_when_workflow_already_present(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.return_value = _content_file("existing")
    op = AddCIWorkflow(name="ci", content=VALID_WORKFLOW)
    diff = op.plan(tmp_db, fake_gh)
    assert all(r.blocked for r in diff.per_repo)
    assert all("workflow already present" in r.summary for r in diff.per_repo)


# ---------- apply ----------


def test_apply_uses_distinct_op_class_in_events(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    """Critical: rollback dispatches on op_class, so AddCIWorkflow events
    must NOT be tagged 'add_file' (or rollback would use the wrong class)."""
    fake_repo.get_contents.side_effect = _missing()
    fake_repo.create_file.return_value = {
        "commit": _commit("c1"),
        "content": _content_file("blob-1"),
    }
    op = AddCIWorkflow(name="ci", content=VALID_WORKFLOW)
    diff = op.plan(tmp_db, fake_gh)
    assert diff.op_class == "add_ci_workflow"

    result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)
    events = get_events_by_op(tmp_db, result.op_id)
    assert all(e["op_class"] == "add_ci_workflow" for e in events)


# ---------- rollback inherited from AddFile ----------


def test_rollback_inherits_blob_safety(
    tmp_db: Database, seed_repos, fake_gh: MagicMock, fake_repo: MagicMock
) -> None:
    fake_repo.get_contents.side_effect = _missing()
    fake_repo.create_file.return_value = {
        "commit": _commit("c1"),
        "content": _content_file("blob-1"),
    }
    op = AddCIWorkflow(name="ci", content=VALID_WORKFLOW)
    diff = op.plan(tmp_db, fake_gh)
    apply_result = op.apply(tmp_db, fake_gh, diff, confirm=lambda _r: True)

    # Rollback path: if blob differs we refuse to delete.
    fake_repo.get_contents.side_effect = None
    fake_repo.get_contents.return_value = _content_file("student-edited")

    result = AddCIWorkflow.rollback(tmp_db, fake_gh, apply_result.op_id)
    assert all(r.status == "skipped_dirty" for r in result.per_repo)
    fake_repo.delete_file.assert_not_called()
