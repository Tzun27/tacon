"""tacon CLI: sync, run, rollback, resume, ui, dashboard, version."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from tacon import __version__
from tacon.classes import (
    ClassesConfigError,
    add_classroom,
    load_classes,
    resolve_db_path,
    set_default,
)
from tacon.classroom import (
    GhClassroomError,
    discover_via_csv,
    discover_via_gh_classroom,
    persist_discovered,
)
from tacon.db import (
    get_events_by_op,
    get_op_class_for_op_id,
    open_db,
    update_event_status,
)
from tacon.github_client import RateLimitedClient
from tacon.ops import ConfirmCallback, Op, RepoDiff, get_op_class, list_ops
from tacon.ops._branch_protection_rule import (
    BranchProtectionRule,
    RuleValidationError,
    load_rule_from_yaml,
    load_rule_template,
)
from tacon.ops.add_branch_protection import AddBranchProtection
from tacon.ops.add_ci_workflow import AddCIWorkflow, WorkflowValidationError
from tacon.ops.add_file import AddFile
from tacon.ops.delete_file import DeleteFile
from tacon.ops.fix_ci_workflow import FixCIWorkflow, make_bump_action_transform

app = typer.Typer(
    name="tacon",
    help="A TA workbench for GitHub Classroom: batch ops + class-health dashboard.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True, style="bold red")


def _default_db_path() -> Path:
    home = Path(os.environ.get("TACON_HOME", Path.home() / ".tacon"))
    home.mkdir(parents=True, exist_ok=True)
    return home / "tacon.db"


def _resolve_db(
    db_path: Path | None,
    classroom: str | None,
) -> Path:
    """Pick the DB path using --db / --classroom / classes.toml / legacy default.

    See ``tacon.classes.resolve_db_path`` for the precedence rules.
    Exits with code 2 on a malformed ``classes.toml`` or unknown
    ``--classroom`` id so the CLI prints a clear error instead of a
    Python traceback.
    """
    try:
        return resolve_db_path(
            explicit_db=db_path,
            classroom_id=classroom,
            default_db=_default_db_path(),
        )
    except ClassesConfigError as exc:
        err_console.print(str(exc))
        raise typer.Exit(2) from exc


# ---------- sync ----------


@app.command()
def sync(
    classroom_id: Annotated[
        str | None,
        typer.Argument(help="GitHub Classroom ID. Required unless --from-csv is set."),
    ] = None,
    from_csv: Annotated[
        Path | None,
        typer.Option(
            "--from-csv",
            help="Fallback roster import. CSV with header: assignment_slug,student_username,repo_url",
        ),
    ] = None,
    db_path: Annotated[Path, typer.Option("--db", help="Path to the tacon SQLite DB.")] = None,  # type: ignore[assignment]
    classroom: Annotated[
        str | None,
        typer.Option(
            "--classroom",
            help="Classroom id from ~/.tacon/classes.toml. "
            "Ignored when --db is given. See `tacon classroom list`.",
        ),
    ] = None,
) -> None:
    """Discover assignments + students + repos and populate the local DB."""
    db = open_db(_resolve_db(db_path, classroom))

    try:
        if from_csv:
            discovered = discover_via_csv(from_csv)
            console.print(f"[green]Imported[/green] {len(discovered)} repos from {from_csv}")
        else:
            if not classroom_id:
                err_console.print("Provide a classroom_id or use --from-csv repos.csv")
                raise typer.Exit(2)
            discovered = discover_via_gh_classroom(classroom_id)
            console.print(f"[green]Discovered[/green] {len(discovered)} repos via gh classroom")
    except GhClassroomError as exc:
        err_console.print(f"sync failed: {exc}")
        if not from_csv:
            err_console.print(
                "Hint: re-run with [bold]--from-csv repos.csv[/bold] to import the roster manually.\n"
                "CSV header: assignment_slug,student_username,repo_url"
            )
        raise typer.Exit(1) from exc

    persist_discovered(db, discovered)
    db_file = db.execute("PRAGMA database_list").fetchone()[2]
    console.print(f"[green]✓[/green] Persisted to {db_file}")


# ---------- run ----------


@app.command()
def run(
    op_name: Annotated[str, typer.Argument(help=f"Op to run. Available: {list_ops()}")],
    path: Annotated[
        str | None,
        typer.Option("--path", help="(add-file/delete-file) Path within each repo."),
    ] = None,
    content_from: Annotated[
        Path | None,
        typer.Option(
            "--content-from",
            help="(add-file/add-ci-workflow) Local file whose content to push.",
        ),
    ] = None,
    workflow_name: Annotated[
        str | None,
        typer.Option(
            "--workflow-name",
            help="(add-ci-workflow/fix-ci-workflow) Workflow filename stem.",
        ),
    ] = None,
    bump_action: Annotated[
        str | None,
        typer.Option(
            "--bump-action",
            help=(
                "(fix-ci-workflow) Replace one action ref with another. "
                "Format: <from>=<to>, e.g. actions/checkout@v3=actions/checkout@v4."
            ),
        ),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option(
            "--branch",
            help="(add-branch-protection) Branch to inspect/protect. Defaults to "
            "each repo's default.",
        ),
    ] = None,
    rule_from: Annotated[
        Path | None,
        typer.Option(
            "--rule-from",
            help="(add-branch-protection) YAML file describing the desired "
            "branch-protection rule. Switches to write mode. Mutually exclusive "
            "with --rule-template.",
        ),
    ] = None,
    rule_template: Annotated[
        str | None,
        typer.Option(
            "--rule-template",
            help="(add-branch-protection) Bundled rule template name. Switches to "
            "write mode. Try `tacon-default` or `strict-pr`.",
        ),
    ] = None,
    message: Annotated[
        str, typer.Option("--message", "-m", help="Commit message.")
    ] = "tacon: add file",
    assignment_id: Annotated[
        str | None,
        typer.Option("--assignment-id", help="Limit to one assignment. Defaults to all."),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply", help="Plan only vs apply.")] = True,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip per-repo confirms.")] = False,
    via_pr: Annotated[
        bool,
        typer.Option(
            "--via-pr",
            help="Open a PR per repo instead of pushing to default branch. "
            "Use for branch-protected classrooms.",
        ),
    ] = False,
    rate: Annotated[float, typer.Option("--rate", help="Max API calls/sec.")] = 3.0,
    db_path: Annotated[Path, typer.Option("--db", help="Path to the tacon SQLite DB.")] = None,  # type: ignore[assignment]
    classroom: Annotated[
        str | None,
        typer.Option(
            "--classroom",
            help="Classroom id from ~/.tacon/classes.toml. "
            "Ignored when --db is given. See `tacon classroom list`.",
        ),
    ] = None,
) -> None:
    """Plan or apply an Op across all active repos in scope."""
    op: Op
    if op_name == "add-file":
        if not path or not content_from:
            err_console.print("add-file requires --path and --content-from")
            raise typer.Exit(2)
        content = content_from.read_text(encoding="utf-8")
        op = AddFile(
            path=path,
            content=content,
            message=message,
            assignment_id=assignment_id,
            via_pr=via_pr,
        )
    elif op_name == "delete-file":
        if not path:
            err_console.print("delete-file requires --path")
            raise typer.Exit(2)
        op = DeleteFile(
            path=path,
            message=message if message != "tacon: add file" else "tacon: delete file",
            assignment_id=assignment_id,
            via_pr=via_pr,
        )
    elif op_name == "add-ci-workflow":
        if not workflow_name or not content_from:
            err_console.print("add-ci-workflow requires --workflow-name and --content-from")
            raise typer.Exit(2)
        content = content_from.read_text(encoding="utf-8")
        try:
            op = AddCIWorkflow(
                name=workflow_name,
                content=content,
                message=message if message != "tacon: add file" else None,
                assignment_id=assignment_id,
                via_pr=via_pr,
            )
        except WorkflowValidationError as exc:
            err_console.print(f"add-ci-workflow: invalid workflow: {exc}")
            raise typer.Exit(2) from exc
    elif op_name == "add-branch-protection":
        if via_pr:
            err_console.print(
                "add-branch-protection: branch protection is repo-level config, "
                "not branch content; --via-pr does not apply. Drop the flag."
            )
            raise typer.Exit(2)
        if rule_from is not None and rule_template is not None:
            err_console.print(
                "add-branch-protection: --rule-from and --rule-template are "
                "mutually exclusive. Pick one."
            )
            raise typer.Exit(2)
        rule: BranchProtectionRule | None = None
        if rule_from is not None:
            try:
                rule = load_rule_from_yaml(rule_from)
            except FileNotFoundError as exc:
                err_console.print(f"--rule-from: {exc}")
                raise typer.Exit(2) from exc
            except RuleValidationError as exc:
                err_console.print(f"--rule-from: {exc}")
                raise typer.Exit(2) from exc
        elif rule_template is not None:
            try:
                rule = load_rule_template(rule_template)
            except RuleValidationError as exc:
                err_console.print(f"--rule-template: {exc}")
                raise typer.Exit(2) from exc
        op = AddBranchProtection(
            branch=branch, assignment_id=assignment_id, rule=rule
        )
    elif op_name == "fix-ci-workflow":
        if not workflow_name or not bump_action:
            err_console.print(
                "fix-ci-workflow requires --workflow-name and --bump-action <from>=<to>"
            )
            raise typer.Exit(2)
        if "=" not in bump_action:
            err_console.print("--bump-action must be <from>=<to>")
            raise typer.Exit(2)
        from_ref, to_ref = bump_action.split("=", 1)
        try:
            transform = make_bump_action_transform(from_ref, to_ref)
        except ValueError as exc:
            err_console.print(f"fix-ci-workflow: {exc}")
            raise typer.Exit(2) from exc
        try:
            op = FixCIWorkflow(
                name=workflow_name,
                transform=transform,
                transform_id=f"bump-action {from_ref}->{to_ref}",
                message=(
                    message if message != "tacon: add file" else "tacon: fix CI workflow"
                ),
                assignment_id=assignment_id,
                via_pr=via_pr,
            )
        except WorkflowValidationError as exc:
            err_console.print(f"fix-ci-workflow: {exc}")
            raise typer.Exit(2) from exc
    else:
        err_console.print(f"Unknown op: {op_name}. Available: {list_ops()}")
        raise typer.Exit(2)

    db = open_db(_resolve_db(db_path, classroom))
    gh = RateLimitedClient(rate_per_sec=rate)

    diff = op.plan(db, gh)
    _print_plan(diff)
    if dry_run:
        console.print("\n[dim](dry run — nothing written. Re-run with --apply to push.)[/dim]")
        return

    confirm = _make_confirm(yes=yes)
    result = op.apply(db, gh, diff, confirm)
    _print_apply_result(result, op_id_label=True)


# ---------- rollback ----------


@app.command()
def rollback(
    op_id: Annotated[str, typer.Argument(help="op_id from a prior apply.")],
    rate: Annotated[float, typer.Option("--rate", help="Max API calls/sec.")] = 3.0,
    db_path: Annotated[Path, typer.Option("--db", help="Path to the tacon SQLite DB.")] = None,  # type: ignore[assignment]
    classroom: Annotated[
        str | None,
        typer.Option(
            "--classroom",
            help="Classroom id from ~/.tacon/classes.toml. "
            "Ignored when --db is given. See `tacon classroom list`.",
        ),
    ] = None,
) -> None:
    """Reverse a prior apply. Blob-SHA-safe: never deletes student work."""
    db = open_db(_resolve_db(db_path, classroom))
    op_class = get_op_class_for_op_id(db, op_id)
    if op_class is None:
        err_console.print(f"No events found for op_id={op_id}")
        raise typer.Exit(1)

    # Map db's op_class field back to a registered op name. Ops use snake_case
    # in events.op_class but are registered with kebab-case in the op registry.
    name_map = {
        "add_file": "add-file",
        "delete_file": "delete-file",
        "add_ci_workflow": "add-ci-workflow",
        "fix_ci_workflow": "fix-ci-workflow",
        "add_branch_protection": "add-branch-protection",
    }
    op_cls = get_op_class(name_map.get(op_class, op_class))

    if not op_cls.supports_rollback:
        err_console.print(f"Op '{op_class}' does not support rollback.")
        raise typer.Exit(1)

    gh = RateLimitedClient(rate_per_sec=rate)
    result = op_cls.rollback(db, gh, op_id)
    if not result.per_repo:
        # No applied events for this op_id — happens when the op only ever
        # produced 'reported' events (a survey, e.g. add-branch-protection
        # without a rule), or when every event was 'skipped'/'failed' so
        # there's nothing to undo. Tell the user clearly rather than
        # printing an empty table.
        err_console.print(
            f"No 'applied' events for op_id={op_id} — nothing to roll back. "
            "(This typically means the op was a read-only survey, or every "
            "repo was skipped/failed at apply time.)"
        )
        raise typer.Exit(1)
    _print_rollback_result(result)


# ---------- resume ----------


@app.command()
def resume(
    op_id: Annotated[str, typer.Argument(help="op_id whose failed events should be retried.")],
    content_from: Annotated[
        Path | None,
        typer.Option(
            "--content-from",
            help="(add-file/add-ci-workflow) Local file whose content to re-push. "
            "Must match the byte length recorded at original apply time.",
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip per-repo confirms.")] = False,
    rate: Annotated[float, typer.Option("--rate", help="Max API calls/sec.")] = 3.0,
    db_path: Annotated[Path, typer.Option("--db", help="Path to the tacon SQLite DB.")] = None,  # type: ignore[assignment]
    classroom: Annotated[
        str | None,
        typer.Option(
            "--classroom",
            help="Classroom id from ~/.tacon/classes.toml. "
            "Ignored when --db is given. See `tacon classroom list`.",
        ),
    ] = None,
) -> None:
    """Re-run apply for repos where the original op left status='failed'.

    Reconstructs the original op from `op_args_json` and replays it against
    only the repos that previously failed. The replay is a brand-new op
    (with its own op_id) — original failed events are kept for audit and
    annotated with a pointer to the resume op_id.
    """
    db = open_db(_resolve_db(db_path, classroom))
    failed = get_events_by_op(db, op_id, status="failed")
    if not failed:
        console.print(f"[yellow]No failed events for op_id={op_id}.[/yellow]")
        return

    op_class = get_op_class_for_op_id(db, op_id)
    if op_class is None:
        err_console.print(f"No events found for op_id={op_id}")
        raise typer.Exit(1)

    try:
        args = json.loads(failed[0]["op_args_json"])
    except json.JSONDecodeError as exc:
        err_console.print(f"resume: malformed op_args_json on op {op_id}: {exc}")
        raise typer.Exit(1) from exc

    op = _reconstruct_op(op_class, args, content_from=content_from)

    gh = RateLimitedClient(rate_per_sec=rate)
    diff = op.plan(db, gh)

    failed_repo_ids = {e["repo_id"] for e in failed}
    diff.per_repo = [r for r in diff.per_repo if r.repo_id in failed_repo_ids]
    if not diff.per_repo:
        err_console.print(
            f"resume: no failed repos are still active (all {len(failed_repo_ids)} archived "
            f"or removed since op {op_id})."
        )
        raise typer.Exit(1)

    console.print(f"[dim]Resuming {len(diff.per_repo)} failed repo(s) from op {op_id}[/dim]")
    _print_plan(diff)

    confirm = _make_confirm(yes=yes)
    result = op.apply(db, gh, diff, confirm)
    _print_apply_result(result, op_id_label=True)
    console.print(f"[dim]Resumed from op_id: {op_id}[/dim]")

    # Annotate the original failed events so the audit trail points at the resume.
    for ev in failed:
        if ev["repo_id"] not in {r.repo_id for r in result.per_repo}:
            continue
        prior_msg = ev.get("error_message") or ""
        suffix = f" (resumed in op_id={result.op_id})"
        if suffix in prior_msg:
            continue
        update_event_status(
            db,
            ev["id"],
            status="failed",
            error_message=(prior_msg + suffix) if prior_msg else suffix.lstrip(),
        )


def _reconstruct_op(
    op_class: str, args: dict[str, object], *, content_from: Path | None
) -> Op:
    """Rebuild an Op instance from stored op_args_json + (optional) content file.

    Used by `tacon resume`. Each branch mirrors the constructor call in `run`,
    sourcing every parameter except content from `args`.
    """
    via_pr = bool(args.get("via_pr", False))
    if op_class == "add_file":
        content = _require_content_from(content_from, op_class, args)
        return AddFile(
            path=str(args["path"]),
            content=content,
            message=str(args.get("message") or "tacon: add file"),
            assignment_id=_opt_str(args.get("assignment_id")),
            via_pr=via_pr,
        )
    if op_class == "delete_file":
        return DeleteFile(
            path=str(args["path"]),
            message=str(args.get("message") or "tacon: delete file"),
            assignment_id=_opt_str(args.get("assignment_id")),
            via_pr=via_pr,
        )
    if op_class == "add_ci_workflow":
        content = _require_content_from(content_from, op_class, args)
        # path is .github/workflows/<name>(.yml|.yaml); strip the directory
        # prefix and let AddCIWorkflow handle the extension.
        path = str(args["path"])
        prefix = ".github/workflows/"
        name = path[len(prefix):] if path.startswith(prefix) else path
        try:
            return AddCIWorkflow(
                name=name,
                content=content,
                message=_opt_str(args.get("message")),
                assignment_id=_opt_str(args.get("assignment_id")),
                via_pr=via_pr,
            )
        except WorkflowValidationError as exc:
            err_console.print(f"resume: stored workflow content failed validation: {exc}")
            raise typer.Exit(2) from exc
    if op_class == "fix_ci_workflow":
        transform_id = str(args.get("transform_id") or "")
        prefix = "bump-action "
        if not transform_id.startswith(prefix) or "->" not in transform_id:
            err_console.print(
                f"resume: cannot reconstruct transform_id={transform_id!r}. "
                "Only `bump-action <from>-><to>` is supported by `tacon resume`. "
                "Re-run via `tacon run fix-ci-workflow ...` if you used a custom transform."
            )
            raise typer.Exit(2)
        from_ref, to_ref = transform_id[len(prefix):].split("->", 1)
        try:
            transform = make_bump_action_transform(from_ref, to_ref)
        except ValueError as exc:
            err_console.print(f"resume: {exc}")
            raise typer.Exit(2) from exc
        return FixCIWorkflow(
            name=str(args["workflow_name"]),
            transform=transform,
            transform_id=transform_id,
            message=str(args.get("message") or "tacon: fix CI workflow"),
            assignment_id=_opt_str(args.get("assignment_id")),
            via_pr=via_pr,
        )
    if op_class == "add_branch_protection":
        # Rebuild the rule from the recorded args dict (None for survey ops,
        # a dict for write ops). from_dict re-validates it on the way in.
        rule_dict = args.get("rule")
        rule_obj: BranchProtectionRule | None = None
        if isinstance(rule_dict, dict):
            from tacon.ops._branch_protection_rule import from_dict as _rule_from_dict

            try:
                rule_obj = _rule_from_dict(rule_dict)
            except RuleValidationError as exc:
                err_console.print(
                    f"resume: stored rule failed re-validation: {exc}. "
                    "If the schema changed since this op was originally run, "
                    "re-run `tacon run add-branch-protection ...` instead."
                )
                raise typer.Exit(2) from exc
        return AddBranchProtection(
            branch=_opt_str(args.get("branch")),
            assignment_id=_opt_str(args.get("assignment_id")),
            rule=rule_obj,
        )

    err_console.print(f"resume: unknown op_class '{op_class}'.")
    raise typer.Exit(1)


def _require_content_from(
    content_from: Path | None, op_class: str, args: dict[str, object]
) -> str:
    """Read --content-from and verify byte length matches the recorded content_len.

    The original AddFile-family ops only store `content_len` in op_args, not the
    bytes themselves (intentional, to keep events small). On resume we trust the
    user to supply the same file; the byte-length check catches the obvious
    "wrong file" mistake without needing a hash.
    """
    if content_from is None:
        err_console.print(
            f"resume: {op_class} requires --content-from <file> "
            "(original content is not stored in the events table)."
        )
        raise typer.Exit(2)
    content = content_from.read_text(encoding="utf-8")
    expected = args.get("content_len")
    if isinstance(expected, int):
        actual = len(content.encode("utf-8"))
        if actual != expected:
            err_console.print(
                f"resume: --content-from byte length {actual} does not match "
                f"original content_len {expected} from op_args. Wrong file?"
            )
            raise typer.Exit(2)
    return content


def _opt_str(value: object) -> str | None:
    """Coerce a JSON-loaded field to Optional[str], preserving None."""
    if value is None:
        return None
    return str(value)


# ---------- ui / dashboard stubs ----------


@app.command()
def ui(
    db_path: Annotated[Path, typer.Option("--db", help="Path to the tacon SQLite DB.")] = None,  # type: ignore[assignment]
    classroom: Annotated[
        str | None,
        typer.Option(
            "--classroom",
            help="Classroom id from ~/.tacon/classes.toml. "
            "Ignored when --db is given. See `tacon classroom list`.",
        ),
    ] = None,
) -> None:
    """Open the read-only TUI: ops on the left, events on the right."""
    try:
        from tacon.tui import TaconApp
    except ImportError as exc:
        # Escape the bracket so rich doesn't parse [tui] as a style tag.
        err_console.print(
            f"TUI unavailable: {exc}. Install with: pip install 'tacon\\[tui]'"
        )
        raise typer.Exit(2) from exc

    app_inst = TaconApp(_resolve_db(db_path, classroom))
    app_inst.run()


@app.command()
def dashboard(
    out: Annotated[Path | None, typer.Option("--out", help="Output dir for static HTML.")] = None,
    publish: Annotated[
        str | None,
        typer.Option(
            "--publish",
            help="<owner>/<repo> to push static site to a branch (default gh-pages). "
            "Use a dedicated dashboard repo (e.g. 'myorg/cs101-dashboard') — "
            "this WILL overwrite the gh-pages branch of whatever repo you point at, "
            "so do not aim it at <username>/<username>.github.io. "
            "The token must have push access to the target.",
        ),
    ] = None,
    publish_branch: Annotated[
        str,
        typer.Option(
            "--publish-branch",
            help="Branch to publish to. Default: gh-pages.",
        ),
    ] = "gh-pages",
    publish_message: Annotated[
        str | None,
        typer.Option(
            "--publish-message",
            help="Override the publish commit message.",
        ),
    ] = None,
    rate: Annotated[float, typer.Option("--rate", help="Max API calls/sec.")] = 3.0,
    db_path: Annotated[Path, typer.Option("--db", help="Path to the tacon SQLite DB.")] = None,  # type: ignore[assignment]
    classroom: Annotated[
        str | None,
        typer.Option(
            "--classroom",
            help="Classroom id from ~/.tacon/classes.toml. "
            "Ignored when --db is given. See `tacon classroom list`.",
        ),
    ] = None,
) -> None:
    """Render the events table to a static HTML dashboard.

    With ``--publish <owner>/<repo>`` the rendered directory is also pushed
    to that repo's ``gh-pages`` branch (configurable via
    ``--publish-branch``). The branch is created if missing; otherwise a
    fresh commit replaces the tree (no incremental patching).
    """
    from tacon.dashboard import PublishError, publish_to_gh_pages, render

    out_dir = out or (Path.cwd() / "tacon-dashboard")
    db = open_db(_resolve_db(db_path, classroom))
    stats = render(db, out_dir)
    console.print(
        f"[green]✓[/green] Rendered {stats['ops']} ops, {stats['events']} events, "
        f"{stats['repos']} repos to [bold]{out_dir}[/bold]"
    )
    console.print(f"[dim]open {out_dir}/index.html in your browser[/dim]")

    if not publish:
        return

    gh = RateLimitedClient(rate_per_sec=rate)
    try:
        result = publish_to_gh_pages(
            gh,
            publish,
            out_dir,
            branch=publish_branch,
            commit_message=publish_message,
        )
    except PublishError as exc:
        err_console.print(f"--publish: {exc}")
        raise typer.Exit(2) from exc

    console.print(
        f"[green]✓[/green] Published {result.files_published} files to "
        f"[bold]{result.target_repo}@{result.branch}[/bold] "
        f"({result.branch_status}, commit {result.commit_sha[:8]})"
    )
    if result.pages_url:
        console.print(
            f"[dim]Pages typically live at[/dim] {result.pages_url} "
            "[dim](custom CNAMEs differ; first publish can take a minute or two)[/dim]"
        )


# ---------- serve (v0.3 GUI) ----------


@app.command()
def serve(
    port: Annotated[
        int | None,
        typer.Option(
            "--port",
            help=(
                "Port to bind. Defaults to the first free port in 5734-5740. "
                "Errors clearly if the requested port is taken."
            ),
        ),
    ] = None,
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help=(
                "Host to bind. Defaults to 127.0.0.1 (loopback only). "
                "The Host-header allowlist (DNS-rebinding defense) only "
                "accepts localhost / 127.0.0.1 / [::1] regardless."
            ),
        ),
    ] = "127.0.0.1",
    open_browser: Annotated[
        bool,
        typer.Option(
            "--open/--no-open",
            help="Open the served URL in the default browser on startup.",
        ),
    ] = True,
) -> None:
    """Start the local web GUI (v0.3 — currently a skeleton, full UI ships in later commits).

    Runs a FastAPI app on localhost. Press Ctrl-C to stop.
    """
    try:
        from tacon.server import PortInUseError
        from tacon.server import serve as run_server
    except ImportError as exc:
        err_console.print(
            f"GUI unavailable: {exc}. Install with: pip install 'tacon\\[gui]'"
        )
        raise typer.Exit(2) from exc

    try:
        run_server(port=port, host=host, open_browser=open_browser)
    except PortInUseError as exc:
        err_console.print(str(exc))
        raise typer.Exit(2) from exc


# ---------- classroom subcommand ----------


classroom_app = typer.Typer(
    name="classroom",
    help="Manage classrooms in ~/.tacon/classes.toml (multi-classroom support).",
    no_args_is_help=True,
)
app.add_typer(classroom_app)


@classroom_app.command("list")
def classroom_list() -> None:
    """Show classrooms registered in ~/.tacon/classes.toml.

    Prints a one-line message and exits if the file doesn't exist (the
    legacy single-DB mode). Use ``tacon classroom add`` to create the
    index by adding a first entry.
    """
    try:
        config = load_classes()
    except ClassesConfigError as exc:
        err_console.print(str(exc))
        raise typer.Exit(2) from exc

    if config is None or not config.classrooms:
        console.print(
            "[dim]No classrooms registered. Run "
            "`tacon classroom add <id> --db PATH` to create the index.[/dim]"
        )
        console.print(f"Legacy default DB: {_default_db_path()}")
        return

    table = Table(title="Classrooms", show_lines=False)
    table.add_column("id")
    table.add_column("db_path")
    table.add_column("description")
    table.add_column("default")
    for cid in config.list_ids():
        c = config.classrooms[cid]
        marker = "[green]*[/green]" if cid == config.default else ""
        table.add_row(cid, c.db_path, c.description, marker)
    console.print(table)


@classroom_app.command("add")
def classroom_add(
    classroom_id: Annotated[
        str, typer.Argument(help="Short id (e.g. 'cs101-spring').")
    ],
    db: Annotated[
        Path, typer.Option("--db", help="Path to the SQLite DB for this classroom.")
    ],
    description: Annotated[
        str, typer.Option("--description", help="Optional human-readable label.")
    ] = "",
    make_default: Annotated[
        bool,
        typer.Option(
            "--default",
            help="Mark this classroom as the default for commands that omit --classroom.",
        ),
    ] = False,
) -> None:
    """Register a classroom in ~/.tacon/classes.toml.

    Re-adding an existing id overwrites its db_path/description (useful
    for moving a classroom DB). If no default is set yet, the first
    added classroom becomes the default automatically.
    """
    try:
        config = add_classroom(
            classroom_id=classroom_id,
            db_path=str(db),
            description=description,
            make_default=make_default,
        )
    except ClassesConfigError as exc:
        err_console.print(str(exc))
        raise typer.Exit(2) from exc
    is_default = config.default == classroom_id
    console.print(
        f"[green]✓[/green] Registered classroom [bold]{classroom_id}[/bold] "
        f"-> {db}" + (" [dim](default)[/dim]" if is_default else "")
    )


@classroom_app.command("set-default")
def classroom_set_default(
    classroom_id: Annotated[str, typer.Argument(help="Classroom id to mark as default.")],
) -> None:
    """Change which classroom is used when --classroom is omitted."""
    try:
        set_default(classroom_id)
    except ClassesConfigError as exc:
        err_console.print(str(exc))
        raise typer.Exit(2) from exc
    console.print(f"[green]✓[/green] Default classroom set to [bold]{classroom_id}[/bold]")


# ---------- version ----------


@app.command()
def version() -> None:
    """Print version + DB path."""
    console.print(f"tacon {__version__}")
    try:
        config = load_classes()
    except ClassesConfigError as exc:
        err_console.print(f"classes.toml: {exc}")
        config = None
    if config and config.classrooms:
        default = config.get_default()
        if default is not None:
            console.print(
                f"db: {default.expanded_db_path()} "
                f"[dim](classroom={default.id})[/dim]"
            )
        else:
            console.print(f"db: {_default_db_path()} [dim](no default classroom set)[/dim]")
    else:
        console.print(f"db: {_default_db_path()}")


# ---------- printers ----------


def _print_plan(diff) -> None:  # type: ignore[no-untyped-def]
    table = Table(title=f"Plan: {diff.op_class}", show_lines=False)
    table.add_column("repo")
    table.add_column("student")
    table.add_column("status")
    table.add_column("summary")
    for r in diff.per_repo:
        status = "[red]BLOCKED[/red]" if r.blocked else "[green]ready[/green]"
        table.add_row(r.repo_id, r.student_id, status, r.summary)
    console.print(table)
    n_ready = sum(1 for r in diff.per_repo if not r.blocked)
    n_blocked = sum(1 for r in diff.per_repo if r.blocked)
    console.print(f"[bold]{n_ready}[/bold] ready, [red]{n_blocked}[/red] blocked")


def _print_apply_result(result, *, op_id_label: bool) -> None:  # type: ignore[no-untyped-def]
    if op_id_label:
        console.print(f"\n[bold]op_id:[/bold] {result.op_id}")
    table = Table(title="Apply result")
    table.add_column("repo")
    table.add_column("status")
    table.add_column("commit_sha")
    table.add_column("error")
    for r in result.per_repo:
        table.add_row(
            r.repo_id,
            r.status,
            (r.commit_sha or "")[:8],
            r.error_message or "",
        )
    console.print(table)
    n_applied = sum(1 for r in result.per_repo if r.status == "applied")
    n_skipped = sum(1 for r in result.per_repo if r.status == "skipped")
    n_failed = sum(1 for r in result.per_repo if r.status == "failed")
    console.print(
        f"[green]{n_applied} applied[/green]   "
        f"[yellow]{n_skipped} skipped[/yellow]   "
        f"[red]{n_failed} failed[/red]"
    )
    if n_failed:
        console.print(f"[dim]To retry just the failed repos: tacon resume {result.op_id}[/dim]")
    if n_applied:
        console.print(f"[dim]To roll everything back: tacon rollback {result.op_id}[/dim]")


def _print_rollback_result(result) -> None:  # type: ignore[no-untyped-def]
    table = Table(title=f"Rollback: op_id={result.op_id}")
    table.add_column("repo")
    table.add_column("status")
    table.add_column("revert_sha")
    table.add_column("note")
    for r in result.per_repo:
        table.add_row(r.repo_id, r.status, (r.revert_sha or "")[:8], r.error_message or "")
    console.print(table)


def _make_confirm(*, yes: bool) -> ConfirmCallback:
    """Build the per-repo confirm callback for the CLI.

    State machine:
      [a] all   -> apply this and every remaining repo without prompting
      [n] no    -> skip this repo, prompt for the next one
      [y] yes   -> apply this repo, prompt for the next one
      [q] quit  -> abort: skip this and every remaining repo
    """
    if yes:
        return lambda _r: True

    state = {"all": False, "quit": False}

    def confirm(repo_diff: RepoDiff) -> bool:
        if state["quit"]:
            return False
        if state["all"]:
            return True
        prompt = (
            f"Apply to [bold]{repo_diff.repo_id}[/bold] ({repo_diff.summary})? "
            "[y]es/[n]o/[a]ll/[q]uit: "
        )
        while True:
            console.print(prompt, end="")
            try:
                answer = input("").strip().lower()
            except EOFError:
                # Non-interactive shell: default to skip everything
                state["quit"] = True
                return False
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no", ""):
                return False
            if answer in ("a", "all"):
                state["all"] = True
                return True
            if answer in ("q", "quit"):
                state["quit"] = True
                return False
            console.print("[dim]respond y/n/a/q[/dim]")

    return confirm


if __name__ == "__main__":
    app()
