"""AddCIWorkflow: push a GitHub Actions workflow file to N student repos.

Same write/rollback semantics as AddFile (subclasses it). Adds:
  - path is constrained to .github/workflows/<name>.yml — caller passes only `name`
  - YAML validation: content must parse and have top-level `on:` and `jobs:`
  - Workflow-aware summary: "+N lines: workflow '<name>' (<job_count> jobs)"

Use cases:
  - Standardize a CI workflow across an entire class
  - Add a missing required-status-check workflow to legacy starter repos
"""

from __future__ import annotations

import re

import yaml

from tacon.ops import register
from tacon.ops.add_file import AddFile

OP_NAME = "add-ci-workflow"
OP_CLASS = "add_ci_workflow"

# Filenames inside .github/workflows/ are loose, but we restrict to a safe charset
# so a bad name can't write outside the workflows directory.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class WorkflowValidationError(ValueError):
    """Raised when the workflow content fails sanity checks."""


class AddCIWorkflow(AddFile):
    """Push a single GitHub Actions workflow to every active repo in scope."""

    op_class_name = OP_CLASS
    default_revert_message = "tacon: add CI workflow"

    def __init__(
        self,
        *,
        name: str,
        content: str,
        message: str | None = None,
        assignment_id: str | None = None,
    ) -> None:
        if not _NAME_RE.match(name):
            raise WorkflowValidationError(
                f"invalid workflow name {name!r}: must match {_NAME_RE.pattern}"
            )
        self.workflow_name = name
        self.job_count = _validate_workflow_yaml(content)

        # YAML files in .github/workflows/ may use .yml or .yaml; we standardize
        # on .yml unless the caller already supplied an extension.
        filename = name if name.endswith((".yml", ".yaml")) else f"{name}.yml"
        path = f".github/workflows/{filename}"

        super().__init__(
            path=path,
            content=content,
            message=message or "tacon: add CI workflow",
            assignment_id=assignment_id,
        )

    # ---------- workflow-aware summary ----------

    def _summary(self, blocked: bool) -> str:
        if blocked:
            return f"BLOCKED: workflow already present at {self.path}"
        line_count = self.content.count("\n") + (
            1 if self.content and not self.content.endswith("\n") else 0
        )
        return (
            f"+{line_count} -0: workflow {self.workflow_name!r} ({self.job_count} jobs)"
        )


def _validate_workflow_yaml(content: str) -> int:
    """Parse + sanity-check workflow YAML. Returns the job count."""
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise WorkflowValidationError(f"workflow YAML did not parse: {e}") from e
    if not isinstance(parsed, dict):
        raise WorkflowValidationError(
            f"workflow YAML must be a mapping at the top level, got {type(parsed).__name__}"
        )
    # PyYAML resolves the bare key `on` to True (YAML 1.1 boolean), so we accept
    # either spelling. Quoted "on" stays as a string.
    if "on" not in parsed and True not in parsed:
        raise WorkflowValidationError("workflow is missing required `on:` trigger block")
    jobs = parsed.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        raise WorkflowValidationError("workflow is missing or has empty `jobs:` block")
    return len(jobs)


# Register on import so cli.rollback / cli.run can find the class by name
register(OP_NAME, AddCIWorkflow)
