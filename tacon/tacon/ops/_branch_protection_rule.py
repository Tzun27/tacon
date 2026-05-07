"""BranchProtectionRule — structured representation of a desired protection state.

Loaded from YAML via ``--rule-from FILE.yaml`` or by name via
``--rule-template tacon-default``. Bundled templates live in
``tacon/templates/protection/`` and are read via importlib.resources so
they work from a wheel install without extra config.

Wire format example::

    required_approving_review_count: 1
    dismiss_stale_reviews: true
    require_code_owner_reviews: false
    required_status_checks: [ci]
    strict_status_checks: false
    enforce_admins: false
    allow_force_pushes: false
    allow_deletions: false
    required_linear_history: false

Unknown keys raise ``RuleValidationError`` (strict — typo'd
``requiered_status_checks`` won't silently no-op).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from importlib import resources
from pathlib import Path
from typing import Any

import yaml


class RuleValidationError(ValueError):
    """Raised when YAML rule content fails validation."""


@dataclass(frozen=True)
class BranchProtectionRule:
    """The protection state we want a branch to end up in."""

    required_approving_review_count: int | None = None
    dismiss_stale_reviews: bool = False
    require_code_owner_reviews: bool = False
    required_status_checks: tuple[str, ...] | None = None
    strict_status_checks: bool = False
    enforce_admins: bool = False
    allow_force_pushes: bool = False
    allow_deletions: bool = False
    required_linear_history: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a YAML/JSON-friendly dict."""
        d = asdict(self)
        # tuple -> list for JSON/YAML cleanliness
        if d["required_status_checks"] is not None:
            d["required_status_checks"] = list(d["required_status_checks"])
        return d

    def to_edit_protection_kwargs(self) -> dict[str, Any]:
        """Map to PyGithub's ``branch.edit_protection(**kwargs)`` parameters.

        PyGithub expects ``contexts=[...]`` (list) for status checks
        rather than a nested object, with ``strict`` as a sibling kwarg.
        Empty / None lists translate to "no status-check requirement"
        (just don't pass the kwarg).
        """
        kw: dict[str, Any] = {
            "dismiss_stale_reviews": self.dismiss_stale_reviews,
            "require_code_owner_reviews": self.require_code_owner_reviews,
            "enforce_admins": self.enforce_admins,
            "allow_force_pushes": self.allow_force_pushes,
            "allow_deletions": self.allow_deletions,
            "required_linear_history": self.required_linear_history,
        }
        if self.required_approving_review_count is not None:
            kw["required_approving_review_count"] = self.required_approving_review_count
        if self.required_status_checks is not None and len(
            self.required_status_checks
        ) > 0:
            kw["contexts"] = list(self.required_status_checks)
            kw["strict"] = self.strict_status_checks
        return kw


_FIELD_NAMES = frozenset(f.name for f in fields(BranchProtectionRule))


def from_dict(data: dict[str, Any]) -> BranchProtectionRule:
    """Build a rule from a parsed dict (the YAML form). Validates keys + types."""
    if not isinstance(data, dict):
        raise RuleValidationError(
            f"protection rule must be a YAML mapping at the top level, "
            f"got {type(data).__name__}"
        )
    unknown = set(data) - _FIELD_NAMES
    if unknown:
        # Sort so the error is deterministic.
        listed = ", ".join(sorted(unknown))
        raise RuleValidationError(
            f"unknown rule key(s): {listed}. "
            f"Valid keys: {', '.join(sorted(_FIELD_NAMES))}"
        )

    # Build kwargs with explicit type coercion + bounds checks.
    kwargs: dict[str, Any] = {}

    if "required_approving_review_count" in data:
        v = data["required_approving_review_count"]
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool):
                raise RuleValidationError(
                    "required_approving_review_count must be an int or null, "
                    f"got {type(v).__name__}"
                )
            if v < 0 or v > 6:
                raise RuleValidationError(
                    "required_approving_review_count must be in [0, 6] (GitHub's "
                    f"valid range), got {v}"
                )
        kwargs["required_approving_review_count"] = v

    for name in (
        "dismiss_stale_reviews",
        "require_code_owner_reviews",
        "strict_status_checks",
        "enforce_admins",
        "allow_force_pushes",
        "allow_deletions",
        "required_linear_history",
    ):
        if name in data:
            v = data[name]
            if not isinstance(v, bool):
                raise RuleValidationError(
                    f"{name} must be a bool, got {type(v).__name__}"
                )
            kwargs[name] = v

    if "required_status_checks" in data:
        v = data["required_status_checks"]
        if v is None:
            kwargs["required_status_checks"] = None
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if not isinstance(item, str) or not item:
                    raise RuleValidationError(
                        f"required_status_checks[{i}] must be a non-empty string, "
                        f"got {item!r}"
                    )
            kwargs["required_status_checks"] = tuple(v)
        else:
            raise RuleValidationError(
                "required_status_checks must be a list of strings or null, "
                f"got {type(v).__name__}"
            )

    return BranchProtectionRule(**kwargs)


def load_rule_from_yaml(path: str | Path) -> BranchProtectionRule:
    """Read a rule from a YAML file on disk."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"rule file not found: {p}")
    try:
        text = p.read_text(encoding="utf-8")
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise RuleValidationError(f"rule YAML did not parse ({p}): {e}") from e
    return from_dict(parsed if parsed is not None else {})


def load_rule_template(name: str) -> BranchProtectionRule:
    """Load a bundled template by name (e.g. ``tacon-default``).

    Templates live at ``tacon/templates/protection/<name>.yaml``. Lookup
    is via importlib.resources so it works from a wheel install.
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise RuleValidationError(
            f"template name must be a simple identifier, got {name!r}"
        )
    filename = name if name.endswith((".yaml", ".yml")) else f"{name}.yaml"
    try:
        ref = resources.files("tacon.templates.protection").joinpath(filename)
    except ModuleNotFoundError as e:
        raise RuleValidationError(
            "tacon.templates.protection package is missing — reinstall tacon"
        ) from e
    if not ref.is_file():
        # List available templates so the error is actionable.
        try:
            available = sorted(
                f.name.removesuffix(".yaml")
                for f in resources.files("tacon.templates.protection").iterdir()
                if f.name.endswith(".yaml")
            )
        except (OSError, AttributeError):
            available = []
        raise RuleValidationError(
            f"unknown rule template: {name!r}. "
            + (
                f"Available: {', '.join(available)}"
                if available
                else "No bundled templates found."
            )
        )
    text = ref.read_text(encoding="utf-8")
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise RuleValidationError(
            f"bundled template {name!r} has malformed YAML: {e}"
        ) from e
    return from_dict(parsed if parsed is not None else {})


def list_bundled_templates() -> list[str]:
    """Return the names of bundled templates (sans .yaml extension)."""
    try:
        return sorted(
            f.name.removesuffix(".yaml")
            for f in resources.files("tacon.templates.protection").iterdir()
            if f.name.endswith(".yaml")
        )
    except (ModuleNotFoundError, OSError, AttributeError):
        return []
