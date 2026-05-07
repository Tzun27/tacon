# Plan — AddBranchProtection write mode (tacon §4.3)

**Status:** LOCKED on 2026-05-07 via batch design Qs (4/4 confirmed defaults).
**Scope:** v0.2 follow-up. Builds on the read-only survey shipped in v0.1.0.
**Companion:** `plans/via_pr.md` is the model for plan structure + pre-flight/apply/rollback prose.

## Problem

`add-branch-protection` is read-only today: it surveys protection state
and records `reported` events. TAs want the inverse — *set* protection
across N repos to a known-good rule (`require 1 PR approval`,
`require status check ci`, etc.) so a class of student repos can be
brought up to a uniform protection bar in one operation.

## Goals

- A write mode for `add-branch-protection` that applies a structured
  `BranchProtectionRule` to each repo's target branch.
- Idempotent: re-running with the same rule on a repo already at that
  state is a no-op (skipped, not failure). Re-running with a different
  rule overwrites — and records the prior state for rollback.
- Rollback restores the prior protection state (if any) per repo. A repo
  that had no protection before goes back to no protection.
- Plays cleanly with the existing scope guard, confirm flow, and event
  schema. One new event-payload column (`prior_state_json`).
- Clear, actionable error when the token lacks admin scope (per-repo
  `permission` classification, same as every other op's API errors).

## Non-goals

- Org-wide rule sets / rule sets API. Per-branch protection only.
- Wildcard branch patterns (`release/*`). Single named branch (default
  branch unless `--branch` is set), same as today's survey.
- `--via-pr` for protection changes. Branch protection is repo-level
  config, not branch content; there is no PR for it. `supports_via_pr`
  stays `False`.
- Protection on multiple branches in one op. Re-run for each branch.
- `--rule` inline-flag soup (`--required-approvals 1 --strict ...`) in
  v0.2. We start with file-only sources; inline can be a v0.2.x
  follow-up if a TA actually wants it.

## Proposed surface

```
tacon run add-branch-protection --rule-from RULE.yaml \
    [--branch main] [--apply --yes]
tacon run add-branch-protection --rule-template strict-pr \
    [--branch main] [--apply --yes]
tacon run add-branch-protection                 # still works: read-only survey, default
```

Mode is determined by presence of `--rule-from` / `--rule-template`. With
neither, the op runs in survey mode (today's behavior — unchanged).
With both, exit 2 with "specify either --rule-from or --rule-template".

`--rule-template <name>` resolves to a YAML bundled at
`tacon/templates/protection/<name>.yaml`. The package ships **at least
one template** so users get a sensible default without writing YAML:

- `tacon-default` — 1 PR review approval, dismiss stale reviews,
  no required status checks, enforce_admins=false.

Both flag forms parse into the same `BranchProtectionRule` dataclass.

## Rule shape

```python
@dataclass(frozen=True)
class BranchProtectionRule:
    """Structured representation of the protection state we want."""
    required_approving_review_count: int | None  # None = no PR-review requirement
    dismiss_stale_reviews: bool = False
    require_code_owner_reviews: bool = False
    required_status_checks: list[str] | None = None  # None = no status-check rqmt
    strict_status_checks: bool = False               # require branches up-to-date
    enforce_admins: bool = False
    allow_force_pushes: bool = False
    allow_deletions: bool = False
    required_linear_history: bool = False
```

YAML wire format (v1, may extend later — fields default if absent):

```yaml
required_approving_review_count: 1
dismiss_stale_reviews: true
require_code_owner_reviews: false
required_status_checks: [ci]      # omit / null = no status-check rqmt
strict_status_checks: false
enforce_admins: false
allow_force_pushes: false
allow_deletions: false
required_linear_history: false
```

Unknown keys raise `RuleValidationError` with the offending keys listed
(strict mode — fail loudly so a typoed `requiered_status_checks` doesn't
silently no-op). Empty `[]` for `required_status_checks` is treated as
"no status-check requirement" (same as `None`/omit).

## Architecture: extend existing op, don't fork

`AddBranchProtection.__init__` gains an optional `rule: BranchProtectionRule
| None`. Default `None` preserves the survey-only behavior. When set, the
op switches to write mode.

The op stays in `tacon/ops/add_branch_protection.py`. Mode is observable
from `args["rule"]` in the events table — `null` → survey, dict → write.

`supports_rollback` becomes `True` **when `rule is not None`**, `False`
otherwise. We expose it as a property:

```python
@property
def supports_rollback(self) -> bool:
    return self.rule is not None
```

Schema impact: a new nullable column `events.prior_state_json TEXT`
holds the per-repo serialized prior protection (the JSON dict that
PyGithub's `branch.get_protection()` returns, or `null` if the branch
was unprotected before). Schema bumps to **v3** with a new
`_migrate_to_v3` function in `db.py` (idempotent, mirrors v2's shape).

## Plan (per repo, write mode)

```
inspect target branch    inspect current protection             render diff           blocked?
─────────────────────    ─────────────────────────             ─────────────         ────────
branch missing       ─►  -                                     synthetic note        yes (branch missing)
already at desired   ─►  read existing + compare to rule       "no change"           yes (idempotent)
mismatch             ─►  read existing → store as prior_state  before/after diff     no
read failed (403)    ─►  -                                     "permission denied"   yes (perm)
```

`_inspect` is reused from survey mode but its return tuple grows: it now
also returns the raw `protection` payload (or None if unprotected) so
plan() can stash a serialized snapshot in the `RepoDiff` for apply() to
persist. No second read between plan and apply — race window is whatever
delay the user takes between `--dry-run` and `--apply`.

## Apply flow per repo (write mode)

```
1. insert event (planned)         (existing)
2. blocked? skipped event          (existing)
3. confirm? skipped event          (existing)
4. branch.edit_protection(...)     (NEW — PyGithub call mapping rule -> kwargs)
5. update_event_status(applied,
   commit_sha=None,                  no commit for an admin action
   prior_state_json=<JSON of prior>)
```

Key PyGithub method: `repo.get_branch(target).edit_protection(...)`.
The rule dataclass maps to its kwargs:

| Rule field                            | edit_protection kwarg            |
|---|---|
| required_approving_review_count       | `required_approving_review_count` |
| dismiss_stale_reviews                 | `dismiss_stale_reviews`           |
| require_code_owner_reviews            | `require_code_owner_reviews`      |
| required_status_checks (list[str])    | `contexts=...`, `strict=...`      |
| strict_status_checks                  | (paired with above)               |
| enforce_admins                        | `enforce_admins`                  |
| allow_force_pushes                    | `allow_force_pushes`              |
| allow_deletions                       | `allow_deletions`                 |
| required_linear_history               | `required_linear_history`         |

Errors:

- 403 → `error_class='permission'` (token lacks admin scope on this repo)
- 404 → `error_class='not_found'` (branch deleted between plan and apply)
- 422 → `error_class='conflict'` (e.g. invalid rule combination per
  GitHub's validation)
- Other GithubException → use `classify_error()`

`commit_sha`/`applied_blob_sha` stay NULL (admin action; no git object).

## Rollback flow per repo

```
event has prior_state_json?      action                                            status
──────────────────────────        ──────                                           ──────
None / null                       remove protection (branch.remove_protection())    rolled_back
{}                                same as null                                      rolled_back
{...} (was protected)             re-apply prior state via edit_protection         rolled_back
prior state is unreachable        skip + diagnostic                                 failed
current state has DRIFTED         leave alone                                      skipped_dirty
   from what we recorded
   as "applied"
```

"Drifted" means the protection rule we wrote is no longer the effective
rule — either someone else changed it, or our applied state was
overridden by an org-wide rule set, etc. We detect drift by re-reading
the current protection at rollback time and comparing it to the rule we
applied (stored in `op_args_json`). Mismatch → `skipped_dirty` with a
clear message that quotes both states. **Same safety stance as
DeleteFile rollback's blob-sha drift check.**

## CLI changes

- New mutually exclusive flags: `--rule-from PATH` and `--rule-template
  NAME`. Either → write mode. Neither → survey mode (default).
- `add-branch-protection --via-pr` continues to exit 2; the rejection
  is no longer "read-only op" but "branch protection is not branch
  content; --via-pr is meaningless here". Unchanged behavior.
- `tacon resume` rehydrates the rule from `op_args_json` (the args dict
  contains `rule: dict`). No `--rule-from` needed at resume time.
- `tacon rollback` works automatically — `supports_rollback` is True for
  write-mode events because `op_args.rule is not None`, False (no-op) for
  survey events.

## Test plan

Unit (target ~30 new tests across 2 files):

- `tests/ops/test_add_branch_protection.py` (existing; extends with):
  - Rule dataclass: validation rejects unknown keys, accepts minimal
    config, accepts maximal config.
  - YAML round-trip (load → dataclass → dump → load equivalence).
  - Plan: write mode shows desired-state diff vs current; idempotent
    when current matches desired (blocked='no change'); branch-missing
    and 403 paths classified correctly.
  - Apply: maps rule → edit_protection kwargs (assert via fake_gh
    call recorder); records prior state; permission/conflict/not_found
    classification.
  - Rollback: prior=null → remove_protection; prior=dict → re-apply;
    drift check triggers skipped_dirty.
- `tests/ops/test_branch_protection_rule.py` (NEW):
  - Dataclass + YAML loader. ~10 tests.

CLI (`tests/test_cli.py`):
- `--rule-from FILE.yaml --apply --yes` runs write mode.
- `--rule-template tacon-default --apply --yes` resolves bundled file.
- Both flags → exit 2.
- File missing → exit 2 with clear error.
- Invalid YAML → exit 2; pinpoints the offending field.

Schema (`tests/test_db.py`):
- `prior_state_json` column present on fresh build at v3.
- v2 → v3 migration is idempotent, doesn't touch existing rows.
- Round-trip a JSON-blob through it.

Live (`tests/live/test_live_branch_protection_write.py`):
- 1 happy-path test: read current state, write a tacon-default rule,
  verify, rollback, verify clean. **`pytest.skip` on 403** so the test
  is harmless on tokens that lack admin scope (the realistic TA case).

Coverage stays ≥90%.

## Schema migration

```python
SCHEMA_VERSION = 3  # was 2

def _migrate_to_v3(db: Database) -> None:
    cols = {c.name for c in db["events"].columns}
    if "prior_state_json" not in cols:
        db["events"].add_column("prior_state_json", str)
```

Idempotent: `add_column` is gated on the cols-set guard, mirroring v2.

## Tooling

- New file `tacon/ops/branch_protection_rule.py` for the dataclass +
  YAML loader. Kept separate from `add_branch_protection.py` so the
  op file stays under ~400 lines.
- New dir `tacon/templates/protection/` for bundled YAML templates.
  Add to `pyproject.toml`'s package-data section so wheels include it.
- `add_branch_protection.py` imports the dataclass + a `template_path()`
  helper that resolves bundled templates via `importlib.resources`.

## Locked design decisions (2026-05-07)

1. **Rule-source surface:** `--rule-from FILE` + `--rule-template
   NAME` only. **No inline flags.** Source-controllable + reproducible
   wins over typeable shortcuts.
2. **Rollback policy:** `supports_rollback=True` whenever `rule is not
   None`. Snapshot prior state to `events.prior_state_json` at apply
   time; restore on rollback. Drift check via re-read at rollback time.
3. **Bundled templates:** ship **two** templates —
   - `tacon-default`: 1 PR review approval, dismiss stale reviews,
     no required status checks, enforce_admins=false.
   - `strict-pr`: 2 PR review approvals, dismiss stale reviews,
     enforce_admins=true, required_linear_history=true.
4. **Live test on missing admin scope:** the live test attempts the
   write; on 403 it `pytest.skip(...)`s with a clear message. Lets it
   run automatically on admin-scoped tokens, harmless on TA tokens.

## Migration / rollout

- Schema bump to v3 happens on first `open_db` after upgrade; no user
  action.
- `tacon run add-branch-protection` (no rule flags) is unchanged. No
  user-facing behavior change for survey-only TAs.
- Existing `events.op_class='add_branch_protection'` rows from prior
  surveys keep working under rollback (no-op because `op_args.rule is
  None` → `supports_rollback` is False).

## Effort estimate

- Schema + migration: ~30 LOC + 4 unit tests
- BranchProtectionRule dataclass + YAML loader: ~80 LOC + 10 unit tests
- Op write-mode apply/rollback + plan extension: ~200 LOC + 15 unit tests
- CLI flags + bundled template: ~50 LOC + 5 CLI tests
- 1 live test (skip-on-403): ~80 LOC
- README update: ~30 LOC

Total: ~470 LOC + ~35 tests across ~6 commits. Comparable to one of the
v0.2 via-pr per-op commits (eafb429 was ~400 LOC).
