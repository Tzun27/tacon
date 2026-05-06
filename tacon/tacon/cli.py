"""tacon CLI: sync, run, rollback, resume. Dashboard + ui are stubs.

Designed to be runnable today. The TUI (`ui`) and dashboard renderer
(`dashboard`) print a "not implemented yet" message pointing to the
design doc — they're scheduled for v0.1.x.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from tacon import __version__
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
)
from tacon.github_client import RateLimitedClient
from tacon.ops import ConfirmCallback, Op, RepoDiff, get_op_class, list_ops
from tacon.ops.add_ci_workflow import AddCIWorkflow, WorkflowValidationError
from tacon.ops.add_file import AddFile
from tacon.ops.delete_file import DeleteFile

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
) -> None:
    """Discover assignments + students + repos and populate the local DB."""
    db = open_db(db_path or _default_db_path())

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
        typer.Option("--workflow-name", help="(add-ci-workflow) Workflow filename stem."),
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
    rate: Annotated[float, typer.Option("--rate", help="Max API calls/sec.")] = 3.0,
    db_path: Annotated[Path, typer.Option("--db", help="Path to the tacon SQLite DB.")] = None,  # type: ignore[assignment]
) -> None:
    """Plan or apply an Op across all active repos in scope."""
    op: Op
    if op_name == "add-file":
        if not path or not content_from:
            err_console.print("add-file requires --path and --content-from")
            raise typer.Exit(2)
        content = content_from.read_text(encoding="utf-8")
        op = AddFile(path=path, content=content, message=message, assignment_id=assignment_id)
    elif op_name == "delete-file":
        if not path:
            err_console.print("delete-file requires --path")
            raise typer.Exit(2)
        op = DeleteFile(
            path=path,
            message=message if message != "tacon: add file" else "tacon: delete file",
            assignment_id=assignment_id,
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
            )
        except WorkflowValidationError as exc:
            err_console.print(f"add-ci-workflow: invalid workflow: {exc}")
            raise typer.Exit(2) from exc
    else:
        err_console.print(f"Unknown op: {op_name}. Available: {list_ops()}")
        raise typer.Exit(2)

    db = open_db(db_path or _default_db_path())
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
) -> None:
    """Reverse a prior apply. Blob-SHA-safe: never deletes student work."""
    db = open_db(db_path or _default_db_path())
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
    }
    op_cls = get_op_class(name_map.get(op_class, op_class))

    if not op_cls.supports_rollback:
        err_console.print(f"Op '{op_class}' does not support rollback.")
        raise typer.Exit(1)

    gh = RateLimitedClient(rate_per_sec=rate)
    result = op_cls.rollback(db, gh, op_id)
    _print_rollback_result(result)


# ---------- resume ----------


@app.command()
def resume(
    op_id: Annotated[str, typer.Argument(help="op_id whose failed events should be retried.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip per-repo confirms.")] = False,
    rate: Annotated[float, typer.Option("--rate", help="Max API calls/sec.")] = 3.0,
    db_path: Annotated[Path, typer.Option("--db", help="Path to the tacon SQLite DB.")] = None,  # type: ignore[assignment]
) -> None:
    """Re-run apply for repos where the original op left status='failed'."""
    db = open_db(db_path or _default_db_path())
    failed = get_events_by_op(db, op_id, status="failed")
    if not failed:
        console.print(f"[yellow]No failed events for op_id={op_id}.[/yellow]")
        return

    op_class = get_op_class_for_op_id(db, op_id)
    if op_class is None:
        err_console.print(f"No events found for op_id={op_id}")
        raise typer.Exit(1)

    # Reconstruct the op from the stored args. For add_file we need content from a re-pass.
    # v0.0.1: only add-file. We require the user to re-supply --content-from for safety
    # (we don't store the raw content).
    err_console.print(
        "resume not yet wired for content reconstruction.\n"
        f"Manual workaround: re-run `tacon run add-file --path <path> --content-from <file> --apply`\n"
        f"and let it skip already-applied repos. Failed repos for op_id={op_id}:"
    )
    table = Table()
    table.add_column("repo_id")
    table.add_column("error_class")
    table.add_column("error_message")
    for ev in failed:
        table.add_row(ev["repo_id"], ev.get("error_class") or "", ev.get("error_message") or "")
    console.print(table)


# ---------- ui / dashboard stubs ----------


@app.command()
def ui() -> None:
    """Textual TUI. Coming in v0.1.x."""
    err_console.print(
        "tacon ui is not implemented yet. The TUI lands in v0.1.x.\n"
        "See: ~/.gstack/projects/gstack-test/tzun-main-design-20260505-155527.md "
        "(Textual layout section)"
    )
    raise typer.Exit(2)


@app.command()
def dashboard(
    out: Annotated[Path | None, typer.Option("--out", help="Output dir for static HTML.")] = None,
    publish: Annotated[
        str | None,
        typer.Option("--publish", help="<owner>/<repo> to push static site to gh-pages."),
    ] = None,
) -> None:
    """Static dashboard renderer. Coming in v0.1.x."""
    err_console.print(
        "tacon dashboard is not implemented yet. The static renderer lands in v0.1.x.\n"
        "See: ~/.gstack/projects/gstack-test/tzun-main-design-20260505-155527.md "
        "(Dashboard render contract section)"
    )
    raise typer.Exit(2)


# ---------- version ----------


@app.command()
def version() -> None:
    """Print version + DB path."""
    console.print(f"tacon {__version__}")
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
