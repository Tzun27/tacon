"""Op ABC + dataclasses.

An Op is a single batch operation across a set of student repos.

The plan/apply/rollback split is load-bearing:
  - plan(db, gh) -> Diff   ......... pure read; powers --dry-run and the diff pane
  - apply(db, gh, diff, confirm) -> ApplyResult  ... writes, gated per-repo by `confirm`
  - rollback(cls, db, gh, op_id) -> RollbackResult  ... reverses a prior apply

`confirm: Callable[[RepoDiff], bool]` is provided by the CLI (line-read y/n/a/q)
or the TUI (modal). Returning True applies; False skips. Both surfaces share
the dataclasses below.

v0.0.1 ships only AddFile (concrete). Future Ops set:
  - requires_clone = True   if they need real git semantics (multi-file merges, rebases)
  - supports_rollback = False if they're inherently irreversible (email, Discord post)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from sqlite_utils import Database

    from tacon.github_client import RateLimitedClient


# ---------- Per-repo and aggregate dataclasses ----------


@dataclass
class RepoDiff:
    """What plan() expects to do to one repo."""

    repo_id: str
    student_id: str
    summary: str  # one-line, e.g. "+12 -0 in STARTER.md"
    unified_diff: str  # full diff text for the diff pane
    blocked: bool = False  # precondition failed (e.g. file already present)
    blocked_reason: str = ""


@dataclass
class Diff:
    """Aggregate plan output for an op invocation."""

    op_class: str
    op_args: dict[str, Any]
    per_repo: list[RepoDiff] = field(default_factory=list)


@dataclass
class RepoApplyResult:
    """Outcome of apply() for one repo."""

    repo_id: str
    status: str  # 'applied' | 'failed' | 'skipped'
    commit_sha: str | None = None
    applied_blob_sha: str | None = None
    error_class: str | None = None
    error_message: str | None = None


@dataclass
class ApplyResult:
    """Aggregate apply output for an op invocation."""

    op_id: str  # UUIDv4 generated at apply() start
    per_repo: list[RepoApplyResult] = field(default_factory=list)


@dataclass
class RepoRollbackResult:
    """Outcome of rollback() for one repo."""

    repo_id: str
    status: str  # 'rolled_back' | 'failed' | 'skipped_dirty' | 'unsupported'
    revert_sha: str | None = None
    error_class: str | None = None
    error_message: str | None = None


@dataclass
class RollbackResult:
    """Aggregate rollback output for an op invocation."""

    op_id: str
    per_repo: list[RepoRollbackResult] = field(default_factory=list)


# ---------- Op ABC ----------


ConfirmCallback = Callable[[RepoDiff], bool]


class Op(ABC):
    """Base class for all batch operations.

    Subclasses set the class-level flags as needed.
    """

    requires_clone: bool = False  # True if the op needs real git semantics
    supports_rollback: bool = True  # False for inherently un-rollbackable ops
    # True if the op accepts `--via-pr` (creates a tacon branch + opens a PR
    # rather than committing directly to default). Read-only ops set False.
    supports_via_pr: bool = False

    @abstractmethod
    def plan(self, db: Database, gh: RateLimitedClient) -> Diff:
        """Read-only. Compute the per-repo diff that apply() would produce."""

    @abstractmethod
    def apply(
        self,
        db: Database,
        gh: RateLimitedClient,
        diff: Diff,
        confirm: ConfirmCallback,
    ) -> ApplyResult:
        """Write. For each non-blocked RepoDiff, call confirm() and act."""

    @classmethod
    def rollback(cls, db: Database, gh: RateLimitedClient, op_id: str) -> RollbackResult:
        """Reverse a prior apply. Default: unsupported.

        Concrete Ops override this. `tacon rollback` checks `supports_rollback`
        before calling and surfaces a clear error if False.
        """
        return RollbackResult(op_id=op_id, per_repo=[])

    @classmethod
    def arg_schema(cls) -> type[BaseModel]:
        """Pydantic model describing this op's ``__init__`` kwargs.

        Powers the v0.3 GUI's auto-generated forms via
        ``arg_schema().model_json_schema()`` → JSON Schema → React form
        components. The same model can be used as a request-body type for
        the FastAPI plan/apply endpoints to get free validation.

        Concrete Ops override this with a Pydantic ``BaseModel`` whose
        field names match the constructor's keyword args. Defaults +
        descriptions + types flow through to the form UI. Field
        descriptions become helper text in the GUI.

        Default: an empty schema (ops without configurable args). The
        GUI surfaces this as a parameter-less op (just classroom picker
        + assignment scope + apply button).
        """

        class EmptyArgs(BaseModel):
            pass

        return EmptyArgs


# ---------- Op registry ----------

# Populated by submodules (e.g. ops/add_file.py) via register() at import time.
# cli.rollback() looks up the class from events.op_class, then calls
# cls.rollback(db, gh, op_id).
_REGISTRY: dict[str, type[Op]] = {}

# Auto-discovery: importing every public submodule of tacon.ops triggers each
# module's register() call. This means a new op file dropped into tacon/ops/
# is available to list_ops() / get_op_class() without touching cli.py.
# Discovery runs lazily on first use (and only once) to avoid import races.
_DISCOVERED: bool = False


def _ensure_discovered() -> None:
    global _DISCOVERED
    if _DISCOVERED:
        return
    # Mark first to make this re-entrant — a submodule's import-time code
    # could conceivably call back into list_ops() / get_op_class().
    _DISCOVERED = True
    import importlib
    import pkgutil

    import tacon.ops as _pkg

    for module_info in pkgutil.iter_modules(_pkg.__path__):
        if module_info.name.startswith("_"):
            continue
        importlib.import_module(f"tacon.ops.{module_info.name}")


def register(name: str, op_cls: type[Op]) -> None:
    if name in _REGISTRY and _REGISTRY[name] is not op_cls:
        raise ValueError(f"Op '{name}' already registered to {_REGISTRY[name]!r}")
    _REGISTRY[name] = op_cls


def get_op_class(name: str) -> type[Op]:
    _ensure_discovered()
    if name not in _REGISTRY:
        raise KeyError(f"Unknown op '{name}'. Registered: {sorted(_REGISTRY.keys())}")
    return _REGISTRY[name]


def list_ops() -> list[str]:
    _ensure_discovered()
    return sorted(_REGISTRY.keys())
