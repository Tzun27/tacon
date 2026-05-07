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
| `add-branch-protection`  | Survey or **set** branch protection rules     |

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

## Scope

v0.1.0:
- ✅ `tacon sync` (gh classroom + CSV fallback)
- ✅ `tacon run` for five ops (`add-file`, `delete-file`,
  `add-ci-workflow`, `fix-ci-workflow`, `add-branch-protection`)
- ✅ `tacon rollback` (blob-SHA-safe)
- ✅ `tacon ui` (Textual TUI: ops on left, events on right)
- ✅ `tacon dashboard --out` (static HTML)

v0.2 (in progress):
- ✅ `tacon resume` finishes its job: replays only the failed repos with
  fresh op_id; original failed events get an audit-trail annotation.
- ✅ `tacon run --via-pr` (described below) for branch-protected classrooms.
- ✅ AddBranchProtection write mode (described below) with snapshot+restore rollback.
- 🚧 `tacon dashboard --publish` (push to gh-pages).

## `--via-pr` mode (branch-protected classrooms)

By default, write ops push directly to each repo's default branch. If
the classroom protects its default branch, you'll see per-repo failures
with `error_class='permission'`. Pass `--via-pr` instead:

```bash
tacon run add-file --path STARTER.md --content-from ./fix.md --via-pr --apply
```

Per repo, `--via-pr`:

1. Creates a branch named `tacon/<op-class>-<op-id-prefix>` at the
   default branch's HEAD.
2. Pushes the file change onto that branch.
3. Opens a PR back into default. The TA reviews + merges.

Each event row records `pr_number` + `pr_branch` (schema v2). Rollback
auto-detects via-pr events and closes the PR + deletes the branch.
**Already-merged PRs are not auto-reverted** — tacon marks them
`skipped_dirty` and tells you to revert manually (auto-revert against
default would require the very write-permission `--via-pr` exists to
avoid).

`--via-pr` does not apply to `add-branch-protection` because branch
protection is repo-level config, not branch content; the CLI rejects
it with exit code 2.

## Branch protection write mode

`add-branch-protection` runs as a read-only survey by default. Pass
`--rule-from FILE.yaml` or `--rule-template <name>` to switch into write
mode and apply the desired rule across every active repo:

```bash
# bundled template (recommended starting point)
tacon run add-branch-protection --rule-template tacon-default --apply --yes

# stricter preset: 2 reviews, dismiss stale, admins enforced, linear history
tacon run add-branch-protection --rule-template strict-pr --apply --yes

# custom rule from a YAML file
tacon run add-branch-protection --rule-from ./my-rule.yaml --apply --yes
```

YAML wire format (everything is optional; defaults are minimal):

```yaml
required_approving_review_count: 1
dismiss_stale_reviews: true
require_code_owner_reviews: false
required_status_checks: [ci, lint]   # null/omit = no requirement
strict_status_checks: false
enforce_admins: false
allow_force_pushes: false
allow_deletions: false
required_linear_history: false
```

Write mode is idempotent (running with the rule already in place is a
no-op) and rollback-safe: each apply records the prior protection state
in `events.prior_state_json`, and `tacon rollback <op-id>` restores it
(or removes protection entirely if the branch was unprotected before).
The rollback drift-checks against the current state and refuses to
clobber if someone else has changed protection since.

**Admin scope required.** Most TA tokens lack admin scope on classroom
repos; non-admin tokens get a per-repo `error_class='permission'`
failure. Run with an admin token (org-level "manage repositories"
permission) to use write mode.

## Limitations

- One classroom per DB. Multi-class config is on the roadmap.
- Live e2e tests cover all five ops (direct + via-pr where relevant).
  Set `TACON_LIVE=1` in `.env` after configuring `TACON_TEST_ORG` +
  `TACON_TEST_ASSIGNMENT_PREFIX` (see `.env.example`) to run them. The
  branch-protection write live test pytest.skips on tokens without
  admin scope, so it's harmless on TA tokens.

## License

MIT
