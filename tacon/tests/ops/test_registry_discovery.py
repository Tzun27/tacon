"""Auto-discovery: a fresh interpreter that only imports `tacon.ops` should
still see every op via list_ops() / get_op_class()."""

from __future__ import annotations

import subprocess
import sys
import textwrap

EXPECTED_OPS = {
    "add-file",
    "delete-file",
    "add-ci-workflow",
    "fix-ci-workflow",
    "add-branch-protection",
}


def _run_in_fresh_interpreter(code: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_registry_is_empty_before_discovery_runs() -> None:
    out = _run_in_fresh_interpreter(
        """
        import tacon.ops as o
        names = sorted(o._REGISTRY)
        print(f"count={len(names)}")
        print(f"discovered={o._DISCOVERED}")
        """
    )
    lines = out.strip().splitlines()
    # Before any list_ops()/get_op_class() call, registry is empty and
    # discovery flag is False.
    assert lines[0] == "count=0", f"unexpected pre-discovery registry: {lines[0]!r}"
    assert lines[1] == "discovered=False"


def test_list_ops_triggers_full_discovery() -> None:
    out = _run_in_fresh_interpreter(
        """
        import tacon.ops as o
        names = o.list_ops()
        print(",".join(names))
        """
    )
    discovered = set(out.strip().split(","))
    missing = EXPECTED_OPS - discovered
    assert not missing, f"auto-discovery did not find: {sorted(missing)}"


def test_get_op_class_triggers_discovery() -> None:
    out = _run_in_fresh_interpreter(
        """
        import tacon.ops as o
        cls = o.get_op_class("add-ci-workflow")
        print(cls.__name__)
        print(cls.op_class_name)
        """
    )
    lines = out.strip().splitlines()
    assert lines[0] == "AddCIWorkflow"
    assert lines[1] == "add_ci_workflow"


def test_discovery_runs_only_once() -> None:
    """If something else gets imported between two registry calls, we don't
    re-discover (the flag stays True after the first run)."""
    out = _run_in_fresh_interpreter(
        """
        import tacon.ops as o
        o.list_ops()
        first = o._DISCOVERED
        # Subsequent calls must be no-ops.
        o.list_ops()
        o.get_op_class("add-file")
        second = o._DISCOVERED
        print(first, second)
        """
    )
    assert out.strip() == "True True"
