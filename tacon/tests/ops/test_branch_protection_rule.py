"""Tests for BranchProtectionRule + YAML/template loaders."""

from __future__ import annotations

from pathlib import Path

import pytest

from tacon.ops._branch_protection_rule import (
    BranchProtectionRule,
    RuleValidationError,
    from_dict,
    list_bundled_templates,
    load_rule_from_yaml,
    load_rule_template,
)

# ---------- dataclass defaults ----------


def test_dataclass_defaults_are_minimal() -> None:
    """A rule with no args means: nothing required, nothing enforced."""
    r = BranchProtectionRule()
    assert r.required_approving_review_count is None
    assert r.dismiss_stale_reviews is False
    assert r.required_status_checks is None
    assert r.enforce_admins is False
    assert r.allow_force_pushes is False
    assert r.allow_deletions is False
    assert r.required_linear_history is False


def test_dataclass_is_frozen() -> None:
    r = BranchProtectionRule()
    with pytest.raises((AttributeError, TypeError)):
        r.enforce_admins = True  # type: ignore[misc]


# ---------- from_dict validation ----------


def test_from_dict_accepts_minimal() -> None:
    r = from_dict({})
    assert r == BranchProtectionRule()


def test_from_dict_accepts_full() -> None:
    r = from_dict(
        {
            "required_approving_review_count": 2,
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": True,
            "required_status_checks": ["ci", "lint"],
            "strict_status_checks": True,
            "enforce_admins": True,
            "allow_force_pushes": False,
            "allow_deletions": False,
            "required_linear_history": True,
        }
    )
    assert r.required_approving_review_count == 2
    assert r.required_status_checks == ("ci", "lint")
    assert r.strict_status_checks is True
    assert r.enforce_admins is True


def test_from_dict_rejects_unknown_key() -> None:
    with pytest.raises(RuleValidationError, match="unknown rule key"):
        from_dict({"requiered_approving_review_count": 1})


def test_from_dict_rejects_bad_type_int_field() -> None:
    with pytest.raises(RuleValidationError, match="required_approving_review_count"):
        from_dict({"required_approving_review_count": "one"})


def test_from_dict_rejects_bool_for_int_field() -> None:
    """Python bool is an int subclass; we explicitly reject it."""
    with pytest.raises(RuleValidationError, match="required_approving_review_count"):
        from_dict({"required_approving_review_count": True})


def test_from_dict_rejects_out_of_range_review_count() -> None:
    with pytest.raises(RuleValidationError, match=r"\[0, 6\]"):
        from_dict({"required_approving_review_count": 7})


def test_from_dict_rejects_bad_type_bool_field() -> None:
    with pytest.raises(RuleValidationError, match="enforce_admins"):
        from_dict({"enforce_admins": "yes"})


def test_from_dict_rejects_bad_status_check_item() -> None:
    with pytest.raises(RuleValidationError, match="required_status_checks"):
        from_dict({"required_status_checks": ["ci", ""]})


def test_from_dict_rejects_status_checks_not_list() -> None:
    with pytest.raises(RuleValidationError, match="required_status_checks"):
        from_dict({"required_status_checks": "ci"})


def test_from_dict_accepts_null_review_count() -> None:
    r = from_dict({"required_approving_review_count": None})
    assert r.required_approving_review_count is None


def test_from_dict_accepts_null_status_checks() -> None:
    r = from_dict({"required_status_checks": None})
    assert r.required_status_checks is None


def test_from_dict_rejects_top_level_non_mapping() -> None:
    with pytest.raises(RuleValidationError, match="mapping"):
        from_dict([1, 2, 3])  # type: ignore[arg-type]


# ---------- to_edit_protection_kwargs ----------


def test_to_edit_protection_kwargs_minimal_omits_status_checks() -> None:
    r = BranchProtectionRule()
    kw = r.to_edit_protection_kwargs()
    assert "contexts" not in kw
    assert "strict" not in kw
    assert "required_approving_review_count" not in kw


def test_to_edit_protection_kwargs_with_status_checks_includes_strict() -> None:
    r = BranchProtectionRule(
        required_status_checks=("ci", "lint"),
        strict_status_checks=True,
    )
    kw = r.to_edit_protection_kwargs()
    assert kw["contexts"] == ["ci", "lint"]
    assert kw["strict"] is True


def test_to_edit_protection_kwargs_empty_status_checks_omits_them() -> None:
    """Empty list = no status-check requirement (no kwargs sent)."""
    r = BranchProtectionRule(required_status_checks=())
    kw = r.to_edit_protection_kwargs()
    assert "contexts" not in kw
    assert "strict" not in kw


def test_to_edit_protection_kwargs_review_count_passes_through() -> None:
    r = BranchProtectionRule(
        required_approving_review_count=2,
        dismiss_stale_reviews=True,
    )
    kw = r.to_edit_protection_kwargs()
    assert kw["required_approving_review_count"] == 2
    assert kw["dismiss_stale_reviews"] is True


# ---------- to_dict round-trip ----------


def test_to_dict_round_trip_via_from_dict() -> None:
    """Anything you put in a rule should survive to_dict -> from_dict."""
    original = BranchProtectionRule(
        required_approving_review_count=1,
        dismiss_stale_reviews=True,
        required_status_checks=("ci",),
        strict_status_checks=True,
        enforce_admins=True,
    )
    rebuilt = from_dict(original.to_dict())
    assert rebuilt == original


# ---------- load_rule_from_yaml ----------


def test_load_rule_from_yaml_happy_path(tmp_path: Path) -> None:
    p = tmp_path / "rule.yaml"
    p.write_text(
        """
required_approving_review_count: 1
dismiss_stale_reviews: true
required_status_checks: [ci]
""",
        encoding="utf-8",
    )
    r = load_rule_from_yaml(p)
    assert r.required_approving_review_count == 1
    assert r.dismiss_stale_reviews is True
    assert r.required_status_checks == ("ci",)


def test_load_rule_from_yaml_missing_file_raises_filenotfound(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        load_rule_from_yaml(tmp_path / "nope.yaml")


def test_load_rule_from_yaml_invalid_yaml_raises_validation(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("required_approving_review_count: [unclosed", encoding="utf-8")
    with pytest.raises(RuleValidationError, match="did not parse"):
        load_rule_from_yaml(p)


def test_load_rule_from_yaml_empty_file_returns_default(tmp_path: Path) -> None:
    p = tmp_path / "empty.yaml"
    p.write_text("", encoding="utf-8")
    r = load_rule_from_yaml(p)
    assert r == BranchProtectionRule()


# ---------- load_rule_template ----------


def test_load_rule_template_rejects_path_traversal() -> None:
    with pytest.raises(RuleValidationError, match="simple identifier"):
        load_rule_template("../../etc/passwd")


def test_load_rule_template_rejects_dotfile() -> None:
    with pytest.raises(RuleValidationError, match="simple identifier"):
        load_rule_template(".secret")


def test_load_rule_template_unknown_lists_available() -> None:
    """When a template name doesn't exist, the error names the available ones
    (when bundled templates exist; otherwise it reports none)."""
    with pytest.raises(RuleValidationError, match="unknown rule template"):
        load_rule_template("does-not-exist")


# ---------- list_bundled_templates ----------


def test_list_bundled_templates_returns_list_or_empty() -> None:
    """Returns a sorted list — empty if no templates have been bundled yet,
    populated once tacon/templates/protection/*.yaml exists."""
    out = list_bundled_templates()
    assert isinstance(out, list)
    assert all(isinstance(name, str) for name in out)
    assert out == sorted(out)


# ---------- bundled templates (tacon-default, strict-pr) ----------


def test_bundled_template_tacon_default_loads() -> None:
    """tacon-default ships with the package and parses to a sensible rule."""
    r = load_rule_template("tacon-default")
    assert r.required_approving_review_count == 1
    assert r.dismiss_stale_reviews is True
    assert r.required_status_checks is None
    assert r.enforce_admins is False
    assert r.required_linear_history is False


def test_bundled_template_strict_pr_loads() -> None:
    """strict-pr is the heavier preset: 2 reviews, admins bound, linear history."""
    r = load_rule_template("strict-pr")
    assert r.required_approving_review_count == 2
    assert r.dismiss_stale_reviews is True
    assert r.enforce_admins is True
    assert r.required_linear_history is True


def test_list_bundled_templates_includes_both() -> None:
    out = list_bundled_templates()
    assert "tacon-default" in out
    assert "strict-pr" in out


def test_load_rule_template_missing_lists_bundled_names() -> None:
    """The error names the available templates so the user can fix the typo."""
    with pytest.raises(RuleValidationError, match="tacon-default") as exc:
        load_rule_template("does-not-exist-yo")
    assert "strict-pr" in str(exc.value)
