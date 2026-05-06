# tacon

A TA workbench for GitHub Classroom: batch operations across student
repos with diff preview, per-repo confirm, rollback, and a static
class-health dashboard your professor can bookmark.

> Status: pre-alpha (0.0.x). Foundation modules + `add_file` Op only.
> Textual TUI and static dashboard land in 0.1.x.

## Why

GitHub Classroom solves grading via CI but does nothing for the TA's
real bottleneck: routine operations across 50+ student repos. Every TA
ends up writing throwaway pygithub scripts. tacon turns those scripts
into a proper workbench, with rollback and an audit trail.

This is deliberately **not** an AI grader or AI tutor. It augments the
TA, never replaces TA judgment.

## Install

```bash
pip install tacon
```

Prereqs:
- Python 3.10+
- `gh` CLI authenticated (`gh auth login`) with scopes `repo`,
  `read:org`, `workflow`. SSO-authorize for the classroom org if
  applicable: `gh auth refresh --scopes repo,read:org,workflow`.
- For roster discovery: the `gh-classroom` extension
  (`gh extension install github/gh-classroom`).

## Quickstart

```bash
# 1. Discover the classroom + students + repos
tacon sync <classroom-id>

# Or, if `gh classroom` doesn't work for you:
tacon sync --from-csv repos.csv  # assignment_slug,student_username,repo_url

# 2. See what an op would do (no writes)
tacon run add-file --path STARTER.md --content-from ./fix.md --dry-run

# 3. Apply with per-repo confirm
tacon run add-file --path STARTER.md --content-from ./fix.md --apply

# 4. Roll back if you regret it
tacon rollback <op-id>

# 5. Or retry just the repos that failed
tacon resume <op-id>
```

## How it works

- One SQLite DB per class (default: `~/.tacon/tacon.db`).
- Each Op has a `plan() → diff` step you can preview.
- Each `apply()` writes events keyed to `student_id`. The audit trail
  is the source of truth.
- Rollback compares blob SHAs (not commit lineage) — tacon will never
  silently delete student work.

## Scope (v0.0.x foundation)

- ✅ `tacon sync` (gh classroom + CSV fallback)
- ✅ `tacon run add-file` (API-only, direct push to default branch)
- ✅ `tacon rollback` (blob-SHA-safe)
- ✅ `tacon resume` (retry only failed events)
- 🚧 `tacon ui` (Textual TUI — coming in 0.1.x)
- 🚧 `tacon dashboard --out` (static HTML — coming in 0.1.x)
- 🚧 `tacon dashboard --publish` (push to gh-pages — coming in 0.1.x)

## Limitations (v0.0.x)

- Direct push to default branch only. Branch-protected classrooms will
  see per-repo failures with `error_class='permission'`. PR-based mode
  (`--via-pr`) lands in 0.1.x or 0.2.x.
- One classroom per DB. Multi-class config in 0.1.x.
- `add_file` is the only Op. `open_issue`, `protect_branch` come later.

## License

MIT
