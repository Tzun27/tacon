# Plan — `--via-pr` mode for write ops (tacon §4.2)

**Status:** REVIEWED via /plan-eng-review on 2026-05-07.
**Scope:** v0.2 follow-up to v0.1.0. Companion to the resume work in `bc247dc`.
**Source-of-truth check:** the canonical design doc at
`~/.gstack/projects/gstack-test/tzun-main-design-20260505-155527.md` line 356
identifies `--via-pr` as a v1.5 feature with branch name `tacon-bot/<op-id>`.
This plan supersedes that branch-name sketch (see §"Branch + PR shape" for
why), but otherwise honors the design's intent.

## Problem

Today every write op (`add-file`, `delete-file`, `add-ci-workflow`,
`fix-ci-workflow`) lands directly on each repo's default branch. Branch-
protected classrooms — which is many of them, by design — fail every
repo with `error_class='permission'`. The TA can see the failures and
roll forward via `tacon resume`, but resume against the same direct-
write path will fail in the same way.

The fix the handoff calls for: a `--via-pr` flag that opens a PR per
repo instead of pushing to default. Apply still records the event;
rollback closes/reverts the PR.

## Goals

- A `--via-pr` flag on `tacon run` that switches every write op from
  "commit to default branch" to "commit to a tacon-owned branch + open
  a PR against default".
- Idempotent: re-running with the same op args against a repo where
  the tacon branch already exists is a no-op (skipped, not failure).
- Rollback semantics that mirror direct-write: undo the change without
  destroying student work or breaking the TA's ability to audit.
- Same per-repo confirm flow + per-repo events as direct-write. Schema
  bump to v2 to track `pr_number` + `pr_branch` (decision below).

## Non-goals

- Merging the PRs from tacon. The TA reviews + merges manually (or via
  a separate workflow). tacon's job is to *propose* the change.
- "Auto-approve" / "auto-merge" flags. Out of scope; can be a v0.2.x
  follow-up.
- Cross-repo PR coordination (e.g. "merge them all once N have CI
  green"). Out of scope.
- Changing the direct-write code path. `--via-pr` is purely additive.
- A `tacon merge <op-id>` command that batch-merges the resulting PRs.
  Natural v0.2.x follow-up; not in this ship.

## Proposed surface

```
tacon run add-file --path X --content-from F --via-pr [--apply --yes]
tacon run delete-file --path X --via-pr [--apply --yes]
tacon run add-ci-workflow --workflow-name ci --content-from ci.yml --via-pr [--apply --yes]
tacon run fix-ci-workflow --workflow-name ci --bump-action ... --via-pr [--apply --yes]
tacon resume <op-id>     # auto-detects via-pr from op_args; no flag needed
tacon rollback <op-id>   # auto-detects via-pr from op_args; closes PRs instead of reverting
```

`--via-pr` on `add-branch-protection` exits 2 with a clear message
("read-only op; --via-pr does not apply"). Each Op subclass declares
`supports_via_pr: bool` (True for the four write ops; False for
read-only). The CLI checks this before constructing the op.

## Architecture decision: constructor-flag, not wrapper-class

I considered a `ViaPR(op)` wrapper that would intercept apply/rollback
to do the branch + PR dance. **Rejected** because:

- AddCIWorkflow subclasses AddFile and inherits its apply/rollback. A
  wrapper that intercepts `apply` on the parent class would have to
  know about the subclass's overrides — fragile.
- The branch + PR dance is op-agnostic at its boundaries (create branch,
  open PR, record event) but op-specific in the middle (the actual
  file write). A wrapper has to either pass through to the wrapped op
  with a `branch=` arg threaded in, or duplicate the writes per op.
  Either way it's the same change to each op's helper as the flag
  approach, just with extra layering.

**Decision: each op takes `via_pr: bool = False` in its constructor.**
A new shared helper module `tacon/ops/_via_pr.py` exposes:

```python
def ensure_branch(gh, repo, op_id, op_class) -> str:
    """Create or no-op-find the tacon branch. Returns branch name."""

def open_or_find_pr(gh, repo, branch, default_branch, title, body, op_id) -> int:
    """Open the PR (or find an existing open one). Returns pr_number."""

def close_pr_and_delete_branch(gh, repo, pr_number, branch, comment) -> None:
    """Rollback inverse. Idempotent: missing PR / missing branch = OK."""

def via_pr_branch_name(op_class: str, op_id: str) -> str:
    """Single source of truth for the branch name format."""
```

Each op's existing `_push_file` / `_patch` / `_delete` gains a
`branch=` parameter that defaults to None (= default branch, i.e.
current behavior). The op's apply() flow becomes:

```python
if self.via_pr:
    branch = ensure_branch(gh, repo, op_id, self.op_class_name)
    if branch is None:  # idempotent skip
        record skipped event; continue
else:
    branch = None  # direct-to-default
commit_sha, blob_sha = self._push_file(gh, repo_id, branch=branch)
if self.via_pr:
    pr_number = open_or_find_pr(gh, repo, branch, ...)
    record event with pr_number + pr_branch
else:
    record event without pr fields
```

The op classes stay shallow — most of the new code lives in `_via_pr.py`.

## Branch + PR shape

Per repo, per op_id:

- **Branch name:** `tacon/<op-class>-<op-id-prefix>`
  - e.g. `tacon/add-file-bc247dc1`
  - 8 hex chars of the op_id keeps the name short + searchable + unique
    across the (at most ~200) repos in one classroom op.
- **Why we diverge from the design doc's `tacon-bot/<full-op-id>`:**
  the design was written before op_class names were finalized; with
  the actual class names in the events table, including op-class in
  the branch name lets a TA scan GitHub's branch list and see what
  kind of change each tacon branch represents without opening the PR.
- **Branch base:** the repo's default branch HEAD at apply time.
- **PR title:** `tacon: <commit-message>` (the same `--message` the
  user passed; e.g. "tacon: fix CI workflow").
- **PR body:** generated; includes:
  1. One-line summary (`op_class` + `op_id`)
  2. The unified diff from plan() (the same one the TUI/CLI shows)
  3. A footer with `op_id` for human correlation with the local DB.
  - **No machine-readable `<!-- tacon-op-id: ... -->` HTML comment.**
    The events table holds `pr_number` so we never parse PR bodies.

## Apply flow per repo

```
plan() blocker (existing)            ensure_branch          (file write)         open_or_find_pr        record event
─────────────────────────            ─────────────          ─────────────        ──────────────         ────────────
file already at path?      ─yes─►  blocked, skipped event (existing path; nothing changes for via-pr)
file absent?               ─no──►  create branch X        →  push file via X  →  open PR Y          →  applied (pr=Y, branch=X)
                                   branch exists @ same SHA → continue (no-op create)
                                   branch exists @ different SHA → skipped_dirty
                                   create_git_ref 422 (other reasons) → failed (error_class=permission|conflict)
```

Step-by-step:

1. Get default branch SHA via `repo.get_branch(default_branch).commit.sha`.
   (Cached per repo; one call per apply, not per op.)
2. `ensure_branch(...)` calls `repo.create_git_ref('refs/heads/<name>', sha)`.
   - `GithubException(422)` "Reference already exists" → check existing
     ref's SHA. If equal: continue. If different: return None (skip).
3. Run the existing op write against the new branch (PyGithub's
   `create_file` / `update_file` / `delete_file` all take `branch=`).
4. `open_or_find_pr(...)` calls `repo.create_pull(title, body, base, head)`.
   - `GithubException(422)` "A pull request already exists for ..." →
     fetch via `repo.get_pulls(state='open', head=f'{repo_owner}:{branch}')`
     and reuse its `number`.
5. Record event with status='applied', pr_number, pr_branch.

## Rollback flow per repo

For an op_id whose events were created via `--via-pr` (pr_number IS NOT NULL):

```
fetch PR state                     branch state              action                          status
─────────────                      ────────────              ──────                          ──────
PR open                            branch exists             close PR + delete branch        rolled_back
PR open                            branch missing            close PR (already half-done)    rolled_back
PR closed (not merged)             branch exists             delete branch                   rolled_back
PR closed (not merged)             branch missing            no-op                           rolled_back
PR merged                          (any)                     refuse                          skipped_dirty + clear msg
PR not found (404)                 (any)                     no-op                           rolled_back
```

The merged-PR case is the hard one. v0.2 chooses the **safe shortcut**:
mark `skipped_dirty` and tell the TA "PR #N merged at <sha>; revert
manually with `git revert <sha>` or open a counter-PR." We don't try
to close-the-revert-loop because:

- Reverting on default branch needs default-branch write access — which
  --via-pr exists to avoid in the first place.
- The student may have committed on top of the merge; auto-revert
  could clobber.
- "skipped_dirty" is the same posture the existing direct-write rollback
  takes when student work is detected — consistent with v0.1 semantics.

## Schema v2

Decision: **bump `meta.schema_version` from 1 to 2**, add two columns
to events. The handoff §4.8 already foresaw a schema v2 (different
reason — FixCIWorkflow rollback latency); we bundle this column add
with that infrastructure.

```sql
ALTER TABLE events ADD COLUMN pr_number INTEGER;        -- NULL for direct-write events
ALTER TABLE events ADD COLUMN pr_branch TEXT;           -- NULL for direct-write events
```

Migration is idempotent and lives in `db.init_db`:

```python
SCHEMA_VERSION = 2

def init_db(db):
    # ... existing v1 table creation ...
    current = get_schema_version(db)
    if current < 2:
        cols = {c.name for c in _t(db, "events").columns}
        if "pr_number" not in cols:
            _t(db, "events").add_column("pr_number", int)
        if "pr_branch" not in cols:
            _t(db, "events").add_column("pr_branch", str)
        _t(db, "meta").update("schema_version", {"value": "2"})
```

`get_schema_version` already exists. The cols-set guard makes the
migration re-runnable on partial states (e.g. crash mid-migration).

`db.insert_event` gains optional `pr_number` and `pr_branch` kwargs.

## Resume + via-pr

`tacon resume <op-id>` already exists (shipped in `bc247dc`). For
via-pr ops, resume should:

1. Read `via_pr` from `op_args_json`.
2. Reconstruct the op with `via_pr=True` (each op's `args` property
   includes `via_pr` so this is mechanical).
3. Replay produces a **new** op_id, hence a **new** branch + PR per
   replayed repo. The original failed events stay annotated
   `(resumed in op_id=Y)` per existing resume semantics.

This means resuming a partially-failed via-pr op leaves orphan branches
in the (originally-failed) repos that already had branch-create
succeed but PR-open fail. That's fine: the next `tacon rollback
<original-op-id>` close/delete-cleans them up. Document this in the
README.

## Plan flow

`plan()` calls per op already do per-repo blocker checks (file already
present, etc.). With `--via-pr`, we add **one** new blocker check:

- Call `repo.get_pulls(state='open', head=f'{repo_owner}:<branch>')`.
  If a PR with the same head exists AND its op_id (parsed from PR
  body) doesn't match the current invocation: blocked with reason
  "PR already open for tacon branch."

Op-class blockers (file exists/absent) stay unchanged. Direct-write
and via-pr semantics map cleanly:

- AddFile via PR: still blocks if file already at path (PR would conflict
  on merge anyway).
- DeleteFile via PR: still blocks if file absent.
- AddCIWorkflow via PR: still blocks if workflow already present.
- FixCIWorkflow via PR: still blocks if workflow absent or transform
  no-ops.

## Test coverage diagram

```
SOURCE FILES                                                    USER FLOWS
[+] tacon/ops/_via_pr.py (new)                                  [+] TA opens PR-mode op against protected classroom
  ├── via_pr_branch_name()       [★★★ TESTED] unit test          ├── [★★  TESTED] CLI integration: 3 repos all 'applied (PR)'
  ├── ensure_branch()                                            ├── [★★★ TESTED] [→E2E] live: real PR opened + cleaned up
  │   ├── [★★★ TESTED] create when absent                        └── [★★  TESTED] CLI integration: 1 repo PR-already-exists → skipped
  │   ├── [★★★ TESTED] no-op when same SHA
  │   ├── [★★★ TESTED] skip when different SHA
  │   └── [★★  TESTED] 422-other → failed
  ├── open_or_find_pr()                                         [+] TA rolls back PR-mode op
  │   ├── [★★★ TESTED] new PR happy path                         ├── [★★  TESTED] all PRs open → all closed + branches deleted
  │   ├── [★★★ TESTED] PR already open → fetch + reuse           ├── [★★  TESTED] one PR merged → that one skipped_dirty
  │   └── [★★  TESTED] non-422 GithubException → failed          └── [★★  TESTED] mixed open/closed → idempotent
  └── close_pr_and_delete_branch()
      ├── [★★★ TESTED] open PR + extant branch                  [+] TA resumes a partially-failed PR-mode op
      ├── [★★★ TESTED] closed PR + extant branch                 └── [★★  TESTED] resume reads via_pr from args, opens fresh PRs
      ├── [★★  TESTED] missing PR (404)
      ├── [★★  TESTED] missing branch (no-op delete)
      └── [★★  TESTED] merged PR → skipped_dirty

[*] tacon/ops/add_file.py
  ├── __init__()                                                [+] Schema v1 → v2 migration
  │   └── [★★★ TESTED] via_pr=True flag                          ├── [★★★ TESTED] open v1 DB, init_db adds pr cols + bumps version
  ├── args (property)                                            ├── [★★★ TESTED] re-running migration is idempotent
  │   └── [★★★ TESTED] includes via_pr key                       └── [★★  TESTED] v0 DB (missing meta) gets v2 schema fresh
  ├── plan() blocked-by-pr check (new)
  │   └── [★★★ TESTED] open PR for our branch → blocked         [+] CLI errors
  ├── _push_file() with branch= kwarg                            ├── [★★  TESTED] add-branch-protection --via-pr exits 2
  │   └── [★★★ TESTED] passes branch through to create_file
  └── apply() with via_pr=True
      ├── [★★★ TESTED] applied: branch+write+PR
      ├── [★★  TESTED] skipped: branch exists with different SHA
      └── [★★  TESTED] failed: PR open errors
  └── rollback() detects pr_number column
      ├── [★★★ TESTED] all open PRs closed + branches gone
      └── [★★  TESTED] mixed-state idempotency

[*] tacon/ops/delete_file.py    same shape as add_file (via_pr branch + 4 unit tests)
[*] tacon/ops/add_ci_workflow.py same shape (subclass; just inherits via_pr support)
[*] tacon/ops/fix_ci_workflow.py same shape

[*] tacon/db.py
  ├── insert_event() new kwargs                                  COVERAGE TARGET: 100% on new code paths
  │   └── [★★★ TESTED] pr_number/pr_branch persist               COVERAGE GATE: 90% (project-wide; current 94.47%)
  └── init_db v1→v2 migration
      ├── [★★★ TESTED] adds columns when absent
      └── [★★★ TESTED] no-op when columns present

[*] tacon/cli.py
  ├── run() --via-pr option                                      Total new tests: ~32-38 (4 ops × 6 unit + ~6 helper + ~4 schema + ~3 CLI)
  │   ├── [★★★ TESTED] threaded into op constructor              Live tests: +3 (one per write op via-pr happy path; AddFile already covered)
  │   └── [★★★ TESTED] rejected for add-branch-protection
  └── resume() reconstructs via_pr from args
      └── [★★  TESTED] _reconstruct_op honors via_pr=True
```

Legend: ★★★ behavior + edge + error  |  ★★ happy path  |  ★ smoke check
[→E2E] = needs live integration test against real GitHub

## Failure modes

| Codepath                            | Failure                                       | Test? | Handling?                                                  | User experience                                          |
|-------------------------------------|-----------------------------------------------|-------|------------------------------------------------------------|----------------------------------------------------------|
| `ensure_branch.create_git_ref`      | branch exists, different SHA                  | yes   | yes (skipped_dirty per-repo, no clobber)                   | clear: "branch exists at <sha>; refusing to overwrite"   |
| `ensure_branch.create_git_ref`      | TA token lacks `repo` scope                   | yes   | yes (mapped to error_class='permission')                   | clear: "permission denied; check `gh auth status`"       |
| `_push_file(branch=...)`            | branch deleted between create + write race    | yes   | yes (caught GithubException 404 → failed)                  | clear per-repo failure with retryable hint               |
| `open_or_find_pr.create_pull`       | PR already open for same head                 | yes   | yes (fetch existing pr_number, reuse)                      | silent: idempotent re-apply lands on same PR             |
| `open_or_find_pr.create_pull`       | head branch already merged + closed           | yes   | yes (caught 422; surfaced as failed not skipped)           | clear per-repo: "PR already merged on prior op; resume" |
| `close_pr_and_delete_branch`        | PR already closed                             | yes   | yes (idempotent)                                           | silent: rollback succeeds                                |
| `close_pr_and_delete_branch`        | PR merged                                     | yes   | yes (skipped_dirty, manual revert hint)                    | clear per-repo: "PR #N merged at <sha>; revert manually" |
| `close_pr_and_delete_branch`        | branch already deleted                        | yes   | yes (idempotent)                                           | silent: rollback succeeds                                |
| `close_pr_and_delete_branch`        | TA token can't delete branches                | yes   | yes (PR closed, branch left; logged in error_message)      | partial: PR closed, branch lingers; not blocking         |
| `init_db v1→v2 migration`           | crash mid-migration (col added, version not)  | yes   | yes (cols-set guard re-checks; safe to re-run)             | silent: next run completes the migration                 |
| `tacon resume` of via-pr op         | via_pr lost from op_args (corrupt JSON)       | yes   | yes (existing JSONDecodeError path → exit 1)               | clear: "malformed op_args_json on op X"                  |

**Critical gaps:** none. Every codepath has a test plan entry, error
handling, and a clear user-visible message.

## What already exists

| Sub-problem                | Existing tacon code that solves it                  | Reused vs rebuilt           |
|----------------------------|----------------------------------------------------|-----------------------------|
| Per-repo event recording   | `db.insert_event` + status state machine            | Reused (add 2 kwargs)        |
| Op base class + dispatch   | `tacon/ops/__init__.py` + auto-discovery            | Reused (add `supports_via_pr`)|
| Confirm callback           | `cli._make_confirm`                                 | Reused as-is                 |
| Plan / apply / rollback split | each op already has these methods                | Reused (extended)            |
| Resume infrastructure      | `cli.resume` + `_reconstruct_op` (shipped bc247dc)  | Reused (`via_pr` flows through args) |
| Schema migration story     | `meta.schema_version` + `init_db` idempotency       | Extended (v1 → v2)            |
| `--apply` / `--yes` flow   | existing `tacon run`                                | Reused as-is                 |
| Live e2e harness           | `tests/live/conftest.py` (scope guard)              | Reused (add 1 via-pr test)   |

Existing direct-write code paths are NOT touched; via-pr is purely
additive. Branch-protected classrooms get a new working flow; existing
classrooms still get the fast direct-write path by default.

## Worktree parallelization strategy

Sequential implementation, no parallelization opportunity worth taking.

The work has tight ordering:
1. Schema v2 + db.insert_event signature (foundation)
2. `tacon/ops/_via_pr.py` helpers (depends on #1 indirectly via testability)
3. AddFile via-pr (depends on #2)
4. DeleteFile / AddCIWorkflow / FixCIWorkflow via-pr (each depends on
   AddFile pattern landing first to copy)
5. CLI wiring + resume reconstruction (depends on op-side code)
6. Live test (depends on full path being green)

Step 4 has 3 mostly-independent ops and could be split across worktrees,
but each is ~30-50 lines + tests; sequential is faster than worktree
overhead at this scale.

## Implementation order (concrete)

Each step is one commit:

1. **Schema v2 migration + db tests.** Bump SCHEMA_VERSION, add cols,
   tests for v0/v1/v2 init paths.
2. **`tacon/ops/_via_pr.py` + unit tests.** All 3 helpers with mocked
   `gh` + `repo`. No CLI yet.
3. **AddFile `--via-pr` apply + rollback.** Add `via_pr` flag, wire
   `_via_pr` helpers into apply/rollback paths, op tests.
4. **DeleteFile / AddCIWorkflow / FixCIWorkflow `--via-pr`.** Mirror
   of #3, one commit per op (3 commits) — keeps each commit reviewable.
5. **CLI wiring.** `tacon run --via-pr`, `add-branch-protection`
   rejection, integration tests.
6. **Resume support.** `_reconstruct_op` threads `via_pr` through;
   resume tests.
7. **README update.** Document via-pr surface, schema v2, the merged-PR
   rollback caveat.
8. **Live e2e test.** One via-pr test against `Netdb-NCKU/pre-test-hw-Tzun27`,
   try/finally cleanup, scope-guarded.

## NOT in scope (v0.2)

Considered during review and explicitly deferred:

- **`tacon merge <op-id>`** to batch-merge resulting PRs. Natural follow-up; not in this ship.
- **Auto-merge / auto-approve flags.** PR review is the TA's call.
- **Cross-repo PR coordination** (e.g. "merge once N have CI green").
- **Classroom-level config to default to via-pr** (vs per-invocation flag). v0.3 if a TA asks.
- **Schema v2 column for FixCIWorkflow.previous_blob_sha** (handoff §4.8). Bundle if cheap, but the rollback-latency concern is independent and can ship in a separate v2.x bump.
- **PR templates / customizable PR titles.** v0.2 hardcodes the format; templating is v0.2.x.
- **Branch protection AUTO-DETECTION** that picks via-pr vs direct-write per repo. v0.3.

## Risks + open questions

1. **PyGithub API surface.** Verified during implementation (will read
   the lib's source for `create_git_ref`, `create_pull`, `pull.edit`,
   `repo.get_git_ref(...).delete()`). All four are stable since
   PyGithub 2.x.
2. **Branch deletion permission.** Acknowledged as soft failure
   (PR closed, branch left) with a logged message in error_message.
   Not a blocker.
3. **Race: student pushes to tacon branch before PR opens.** Branch
   name has op-id prefix → effectively impossible. The PR would just
   include the student's commit; defensible.
4. **Live test side-effects.** Each via-pr live test run opens AND
   closes a real PR in the test repo. `try/finally` cleanup keeps
   state clean. Each run leaves a tiny audit-trail commit pair
   (similar to existing AddFile live tests).
5. **Schema migration on a DB written by tacon 0.1.x.** Tested.
   The `meta.schema_version` path + cols-set guard handles all states.
6. **Concurrent `tacon run` invocations.** Already not safe in v0.1
   (no DB lock); not a regression. Document in README.

## TODOS captured for v0.2.x+

- `tacon merge <op-id>`: batch-merge PRs after CI passes.
- Auto-detect branch protection per repo and pick via-pr vs direct.
- Live e2e for delete-file / add-ci-workflow / fix-ci-workflow
  (handoff §4.7); via-pr makes these viable on the protected test repo.
- `previous_blob_sha` column for fast FixCIWorkflow rollback (handoff §4.8).

## Completion summary

- Step 0 Scope Challenge: scope accepted as-is (gate did not trigger; ~6 files, 0-1 new helper module, 0 new services).
- Architecture review: 3 issues surfaced — branch name (resolved), wrapper-vs-flag (decided constructor-flag), schema bump (decided v2). All resolved in the plan above.
- Code Quality review: 1 issue — PR-body machine-readable marker is redundant given pr_number in events; **dropped from plan**.
- Test review: diagram produced; ~32-38 unit tests + 1 live test; 0 critical gaps; 100% target coverage on new code paths.
- Performance review: no concerns. PR open + branch create are 2 extra API calls per repo; 50-repo apply still well under the 5000/hr GitHub primary rate limit. Rate-limited client already handles secondary limits.
- NOT in scope: written.
- What already exists: written.
- Failure modes: written; 0 critical gaps.
- Outside voice: skipped (this is an internal tooling extension; the existing CEO/eng review on the parent design covered the strategy and architecture in May 2026).
- Parallelization: 0 lanes / sequential.
- Lake Score: 8/8 — every test path has a test, every error path has handling.

## Unresolved decisions

None.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run (covered by parent design office-hours 2026-05-05) |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 4 issues, 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | not applicable (no UI surface) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | n/a |
| Outside Voice | `/codex-plan-review` | Independent 2nd opinion | 0 | — | skipped |

**UNRESOLVED:** 0 — every architectural decision is committed.

**VERDICT:** ENG CLEARED — ready to implement.
