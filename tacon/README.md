# tacon

A TA workbench for GitHub Classroom: batch operations across student
repos with diff preview, per-repo confirm, rollback, and a static
class-health dashboard your professor can bookmark.

> Status: alpha (0.1.x). Five ops, Textual TUI, static dashboard. Live
> end-to-end test against the GitHub API still TODO.

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

# 6. Browse the audit trail (TUI)
tacon ui

# 7. Render a shareable static HTML dashboard
tacon dashboard --out ./tacon-site
# open ./tacon-site/index.html
```

## Available ops

| Op                       | Description                                    |
|--------------------------|------------------------------------------------|
| `add-file`               | Push a single file to N repos                  |
| `delete-file`            | Remove a file from N repos                     |
| `add-ci-workflow`        | Push a `.github/workflows/<name>.yml`          |
| `fix-ci-workflow`        | Patch an existing workflow (e.g. bump action) |
| `add-branch-protection`  | Read-only survey of branch protection state   |

Every op supports plan/apply/rollback (where rollback is meaningful) with
the same blob-SHA-based safety: tacon refuses to overwrite work it didn't
write itself.

## How it works

- One SQLite DB per class (default: `~/.tacon/tacon.db`).
- Each Op has a `plan() → diff` step you can preview.
- Each `apply()` writes events keyed to `student_id`. The audit trail
  is the source of truth.
- Rollback compares blob SHAs (not commit lineage) — tacon will never
  silently delete student work.

## Scope (v0.1.0)

- ✅ `tacon sync` (gh classroom + CSV fallback)
- ✅ `tacon run` for five ops (`add-file`, `delete-file`,
  `add-ci-workflow`, `fix-ci-workflow`, `add-branch-protection`)
- ✅ `tacon rollback` (blob-SHA-safe)
- ✅ `tacon resume` (retry only failed events; partial — see Limitations)
- ✅ `tacon ui` (Textual TUI: ops on left, events on right)
- ✅ `tacon dashboard --out` (static HTML)
- 🚧 `tacon dashboard --publish` (push to gh-pages — coming in 0.1.x)

## Limitations (v0.1.0)

- Direct push to default branch only. Branch-protected classrooms will
  see per-repo failures with `error_class='permission'`. PR-based mode
  (`--via-pr`) lands in 0.2.x.
- One classroom per DB. Multi-class config in 0.2.x.
- `tacon resume` for `add-file` requires you to re-supply
  `--content-from` (we don't store the raw bytes); it currently prints
  the failed repo list and the manual workaround.
- `add-branch-protection` is read-only. Write-mode requires admin token
  and is planned for 0.2.x.
- No live end-to-end test against api.github.com yet — all tests mock
  PyGithub. Set up via `--live` pytest marker is on the roadmap.

## License

MIT
