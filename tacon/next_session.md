# Next session — pick up tacon mid-v0.2 (after `--via-pr`)

You (the next agent) are continuing work on **tacon**, a TA workbench for
GitHub Classroom. v0.1.0 shipped + three v0.2 items shipped on top
(see §-1). What's left for v0.2 either needs design input from the user
or is genuinely lower priority — that's why this session paused.

This file is your handoff. **Read it top-to-bottom before doing anything
else.** §-1 (what shipped this session) and §4 (what's left) are the
two parts you can't skip.

---

## -1 · What shipped after v0.1.0

**Sixteen commits since the v0.1.0 handoff** (`7c181e5`):

| Commit | Item | What |
|---|---|---|
| `bc247dc` | §4.1 | `tacon resume` finished. Reconstructs the op from `op_args_json`, replays apply on only the originally-failed repos, gets a fresh op_id, annotates the original failed events with `(resumed in op_id=Y)`. add-file/add-ci-workflow require `--content-from`; reject byte-length mismatches. fix-ci-workflow re-derives the bump-action transform from `transform_id`. delete-file and add-branch-protection need no extra flags. |
| `7db9398` | §4.5 | PyPI publish prep: `python -m build` produces a clean wheel + sdist, dashboard templates land in `tacon/dashboard/templates/`, console-script `tacon` resolves, `tacon dashboard --out` renders cleanly from a fresh-venv install. **Bug found+fixed:** `pip install 'tacon[tui]'` was being mangled by rich (it parsed `[tui]` as a malformed style tag and stripped it). Bracket now escaped. |
| `75bc0a2` | docs | Refreshed this handoff doc for the resume + PyPI prep work. |
| `04067fe` | §4.2 | **Schema v2** — added `events.pr_number` + `events.pr_branch` columns. Idempotent migration in `init_db._migrate_to_v2` with cols-set guard so partial-state recovery just works. |
| `7e6537a` | §4.2 | **`tacon/ops/_via_pr.py`** — branch + PR helpers. `via_pr_branch_name`, `ensure_branch` (raises `BranchConflictError` on different-SHA), `open_or_find_pr` (recovers existing PR on 422), `close_pr_and_delete_branch` (returns `RollbackOutcome` with merged-PR → `skipped_dirty`). `_` prefix excludes from auto-discovery. |
| `7ba028f` | §4.2 | **AddFile `--via-pr`** — apply + rollback. New `Op.supports_via_pr: bool` ABC flag (True on AddFile/DeleteFile/AddCIWorkflow/FixCIWorkflow; False on AddBranchProtection). `update_event_status` and `insert_event` gained `pr_number`/`pr_branch` kwargs. |
| `19eee55` | §4.2 | **DeleteFile `--via-pr`** — same shape as AddFile; `_delete` accepts `branch=` kwarg. |
| `eafb429` | §4.2 | **AddCIWorkflow + FixCIWorkflow `--via-pr`** — AddCIWorkflow inherits AddFile's via-pr machinery wholesale (just forwards `via_pr` through `__init__`). FixCIWorkflow has its own apply path; `_patch` accepts `branch=` kwarg, `_apply_via_pr` orchestrates branch+patch+PR. |
| `82c1174` | §4.2 | **CLI `--via-pr`** — flag added to `tacon run`; threaded into all four write ops; `add-branch-protection --via-pr` exits 2 with "read-only op" message. |
| `5c75ffe` | §4.2 | **`tacon resume` for via-pr ops** — `_reconstruct_op` reads `via_pr` from the op_args dict (each op's `args` already includes it) and forwards to the constructor. Resume of a via-pr op gets a fresh op_id + fresh branch + fresh PR per replayed repo. |
| `16d168b` | §4.2, docs | README documents `--via-pr`; new `tests/live/test_live_via_pr.py` exercises the full apply→PR→rollback round-trip against the real test repo with try/finally cleanup. |
| `0276b73` | §4.7 | **Live e2e: DeleteFile direct-write** — seeds a tacon-marked file via the API, runs DeleteFile.plan→apply→verify gone→rollback→verify restored. Plus blocked-when-path-absent companion. |
| `45d2587` | §4.7 | **Live e2e: DeleteFile `--via-pr`** — seeds on default, verifies file goes away on tacon branch but not on default + PR opens; rollback closes PR + deletes branch + leaves default untouched. |
| `724039e` | §4.7 | **Live e2e: AddCIWorkflow direct + `--via-pr`** — two tests writing a unique tacon-marked workflow file under `.github/workflows/`. |
| `10ed37e` | §4.7 | **Live e2e: FixCIWorkflow direct + `--via-pr`** — seeds workflow with `actions/checkout@v3`, bumps to v4 via `make_bump_action_transform`, verifies bump landed (or only on branch for via-pr), rolls back. |
| `45731c0` | §4.7 | **Live e2e: AddBranchProtection survey** — read-only plan+apply against the test repo; verifies summary shape + event lands with status `reported`. |

Four v0.2 items now shipped: §4.1 (resume), §4.2 (`--via-pr`), §4.5
(PyPI prep), §4.7 (live e2e for the remaining ops). What's left is in §4 below.

---

## 0 · Orient yourself (5 min)

```bash
cd /home/tzun/repos/tacon/tacon
git status --short                   # clean (or just .gitignored .env / .venv / dist)
git log --oneline | head -5          # confirm 45731c0 is the tip
.venv/bin/pytest -q --no-cov         # 242 unit tests pass in ~17s
.venv/bin/ruff check . && .venv/bin/mypy tacon   # both clean
```

Expected:
- `45731c0 test(tacon): live e2e for AddBranchProtection read-only survey` is the tip of `main`.
- 242 unit tests pass; **18 live tests** pass **if the user's `.env`
  has `TACON_LIVE=1`** (10 from prior session + 8 new from §4.7). Live
  tests hit the real GitHub API.
- Coverage stays above 90% (gate). ruff + mypy clean across **16
  source files**.

If any of that fails, **stop and investigate before continuing** — something
has drifted since this handoff was written.

> **Drift caught last session:** the venv had lost its dev extras
> (`textual`, `pytest-asyncio`, `types-pyyaml`) and even the editable
> install was at the old `tacon 0.0.1` version. If pytest collection
> fails with `ModuleNotFoundError: No module named 'textual'`, run
> `.venv/bin/pip install -e ".[dev]"` to fix.

> **HEADS-UP about live tests + the test repo's history.** When
> `TACON_LIVE=1` (the user's `.env` typically has this), every full
> pytest run does:
> - One **direct-write** apply+rollback on `Netdb-NCKU/pre-test-hw-Tzun27`
>   → leaves an apply commit + revert commit pair on `main`.
> - One **via-pr** apply+rollback (new this session) → opens a PR,
>   closes it (not merged), deletes the tacon branch. The closed PR
>   stays in the repo's PR history; the branch is gone.
>
> If the user doesn't want the noise, flip `TACON_LIVE=0` in
> `tacon/.env` (gitignored — safe to tell them how to edit it but
> **never read or echo its contents** since it has the token).

---

## 1 · What exists right now (don't rebuild)

### Code (tacon/)

```
tacon/
├── __init__.py                       __version__ = "0.1.0"
├── cli.py                            Typer app: sync, run (+ --via-pr),
│                                     rollback, resume (+ --content-from),
│                                     ui, dashboard, version
├── classroom.py                      gh classroom + CSV roster discovery
├── db.py                             SQLite: assignments, students, repos,
│                                     events, interactions, meta
│                                     SCHEMA_VERSION = 2 (events.pr_number
│                                     + pr_branch added; idempotent migration
│                                     in `_migrate_to_v2`)
├── github_client.py                  RateLimitedClient (Auth.Token) + 8-class
│                                     classify_error() + retry-after parsing
├── ops/
│   ├── __init__.py                   Op ABC + 6 dataclasses + lazy
│   │                                 pkgutil-based registry auto-discovery.
│   │                                 New: `Op.supports_via_pr: bool` flag.
│   ├── _via_pr.py                    Branch + PR helpers (NEW v0.2). The `_`
│   │                                 prefix excludes from auto-discovery.
│   │                                 Exports: via_pr_branch_name,
│   │                                 ensure_branch (raises BranchConflictError),
│   │                                 open_or_find_pr (idempotent on 422),
│   │                                 close_pr_and_delete_branch (returns
│   │                                 RollbackOutcome).
│   ├── add_file.py                   AddFile (supports_via_pr=True). Has
│   │                                 _ApplyOutcome dataclass, _apply_via_pr,
│   │                                 _rollback_via_pr; _push_file accepts
│   │                                 branch= kwarg.
│   ├── delete_file.py                DeleteFile (supports_via_pr=True). Same
│   │                                 shape: _apply_via_pr, _rollback_via_pr;
│   │                                 _delete accepts branch= kwarg.
│   ├── add_ci_workflow.py            AddCIWorkflow (subclasses AddFile;
│   │                                 inherits via-pr wholesale; just
│   │                                 forwards via_pr through __init__).
│   ├── fix_ci_workflow.py            FixCIWorkflow (supports_via_pr=True).
│   │                                 Own apply path; _patch accepts branch=,
│   │                                 _apply_via_pr orchestrates,
│   │                                 _rollback_via_pr handles.
│   └── add_branch_protection.py      Read-only survey
│                                     (supports_rollback=False,
│                                      supports_via_pr=False).
├── dashboard/
│   ├── __init__.py                   re-exports render
│   ├── render.py                     Jinja2 → static HTML
│   └── templates/
│       ├── base.html, index.html, op.html, repo.html
└── tui/
    ├── __init__.py                   re-exports TaconApp
    └── app.py                        Textual TUI
```

Five ops registered: `add-file`, `delete-file`, `add-ci-workflow`,
`fix-ci-workflow`, `add-branch-protection`. Auto-discovered (drop a new
file in `tacon/ops/` and call `register("name", Cls)` at the bottom).
**Modules starting with `_` are explicitly skipped** — that's why
`_via_pr.py` is helpers, not an op.

### Tests (tests/, 242 unit + 18 live)

- `tests/conftest.py` — `tmp_db`, `seed_repos`, `fake_repo`, `fake_gh`
- `tests/test_db.py` — 18 tests (5 new for schema v2: fresh-build cols,
  v1→v2 in-place migration, re-run idempotency, partial-state recovery,
  pr fields round-trip)
- `tests/test_github_client.py` — 43 tests
- `tests/test_classroom.py` — 11 CSV + gh shell-out tests
- `tests/test_cli.py` — 43 integration tests (via-pr CLI flag, resume of
  via-pr ops, add-branch-protection --via-pr rejection added)
- `tests/test_dashboard.py` — 12 tests
- `tests/test_tui.py` — 6 Textual Pilot tests
- `tests/ops/test_via_pr.py` — **18 helper unit tests (NEW)**
- `tests/ops/test_add_file.py` — 20 tests (7 new for via-pr)
- `tests/ops/test_delete_file.py` — 17 tests (5 new for via-pr)
- `tests/ops/test_add_ci_workflow.py` — 16 tests (2 new for via-pr inheritance)
- `tests/ops/test_fix_ci_workflow.py` — 23 tests (5 new for via-pr)
- `tests/ops/test_add_branch_protection.py` — unchanged
- `tests/ops/test_registry_discovery.py` — unchanged
- `tests/live/` — opt-in live e2e (18 tests):
  - `test_live_read.py` (7 tests, read-only)
  - `test_live_apply_rollback.py` (2 tests, AddFile direct write+rollback)
  - `test_live_via_pr.py` (1 test, AddFile via-pr round-trip)
  - `test_live_delete_file.py` (2 tests, DeleteFile direct + blocked-when-absent)
  - `test_live_delete_file_via_pr.py` (1 test, DeleteFile via-pr round-trip)
  - `test_live_add_ci_workflow.py` (2 tests, AddCIWorkflow direct + via-pr)
  - `test_live_fix_ci_workflow.py` (2 tests, FixCIWorkflow direct + via-pr)
  - `test_live_add_branch_protection.py` (1 test, read-only survey)

### CLI surface (current)

```
tacon sync <classroom-id>
tacon sync --from-csv repos.csv
tacon run add-file --path X --content-from F [--via-pr] [--apply --yes]
tacon run delete-file --path X [--via-pr] [--apply --yes]
tacon run add-ci-workflow --workflow-name ci --content-from ci.yml \
    [--via-pr] [--apply --yes]
tacon run fix-ci-workflow --workflow-name ci \
    --bump-action actions/checkout@v3=actions/checkout@v4 \
    [--via-pr] [--apply --yes]
tacon run add-branch-protection [--branch main] [--apply --yes]
                                # --via-pr is REJECTED here (read-only op)
tacon rollback <op-id>          # auto-detects via-pr events from pr_number
tacon resume <op-id> [--content-from FILE] [--yes]
                                # auto-detects via-pr from op_args.via_pr
tacon ui                        # Textual TUI
tacon dashboard --out ./site    # static HTML
tacon dashboard --publish ...   # NOT YET WIRED — exit 2 (still §4.4)
tacon version
```

### Tooling

- `pyproject.toml` — name `tacon`, version `0.1.0` (NOT bumped this
  session; v0.2 is mid-flight). Deps unchanged. Coverage gate
  `--cov-fail-under=90`. `asyncio_mode = "auto"`.
- `.github/workflows/ci.yml` — matrix on Python 3.10/3.11/3.12.
- `.venv/` — Python 3.13.5 (dev extras: `pip install -e ".[dev]"`).
- `.env` (gitignored) — live test config + GitHub token.
- `.env.example` — uses `Netdb-NCKU/pre-test-hw-Tzun27` as the worked example.
- `plans/via_pr.md` — the eng-reviewed plan that drove this session's
  via-pr work. Read this if you want the architectural rationale
  (constructor-flag vs wrapper-class, branch-name divergence from the
  design doc, merged-PR rollback policy).

---

## 2 · Live e2e tests (the safety story)

Read this before touching anything that talks to real GitHub.

**Configuration is in `tacon/.env`** (gitignored). It typically contains:
- `TACON_GITHUB_TOKEN` — user's GitHub PAT (scoped to repo + their org)
- `TACON_TEST_ORG=Netdb-NCKU`
- `TACON_TEST_ASSIGNMENT_PREFIX=pre-test-hw`
- `TACON_TEST_REPO=Netdb-NCKU/pre-test-hw-Tzun27` (pinned to user's own repo)
- `TACON_LIVE=1` (live tests enabled)

**Hard scope guard:** `tests/live/conftest.py::assert_in_scope(repo_full_name)`
raises `OutOfScopeError` if the repo isn't in `TACON_TEST_ORG` or
doesn't carry `TACON_TEST_ASSIGNMENT_PREFIX` in its name. Every live
test calls this before any API interaction. **Do not loosen it.** If a
future test needs broader scope, talk to the user first.

**Live tests:**
- `test_live_read.py` (7 tests, read-only) — token auth, org visibility,
  repo discovery, single-repo metadata read, three scope-guard self-tests.
- `test_live_apply_rollback.py` (2 tests, write+rollback) — direct-write
  AddFile.plan → apply → verify → rollback → verify-clean cycle on a
  unique tacon-marked path; plus a blocked-when-present test against a
  real README.
- `test_live_via_pr.py` (1 test, write+rollback with PR) — `AddFile(via_pr=True)`
  end-to-end: branch created at default-branch HEAD, file written ON the
  branch (NOT default), PR opened, rollback closes the PR (not merged)
  + deletes the branch. `try/finally` cleanup closes any open PR + deletes
  any leftover branch even if the test crashes mid-flight.

To run only live tests: `.venv/bin/pytest tests/live/ -v --no-cov`.
To skip live during a quick run: prepend `TACON_LIVE=0`.

---

## 3 · `--via-pr` quick-reference (so you don't have to grep)

Per-repo flow when `--via-pr` is set:

```
plan blocker (existing)        ensure_branch       (file write w/ branch=)     open_or_find_pr      record event
─────────────────────         ─────────────       ──────────────────────       ──────────────       ────────────
(same as direct-write)   ──►  create branch X  →  push file via X            →  open PR Y         →  applied (pr=Y, branch=X)
                              branch exists @ same SHA → continue (idempotent)
                              branch exists @ different SHA → BranchConflictError → skipped (error_class='conflict')
                              other create_git_ref errors → failed (per classify_error)
```

Rollback (auto-detected by `pr_number IS NOT NULL` on the event):

```
PR state         branch state    action                       status
───────────      ────────────    ──────                       ──────
open             exists          close + delete branch        rolled_back
open             missing         close                        rolled_back
closed (!merged) exists          delete branch                rolled_back
closed (!merged) missing         no-op                        rolled_back
merged           (any)           refuse + manual-revert hint  skipped_dirty
not_found (404)  (any)           best-effort branch delete    rolled_back
```

**Branch name format:** `tacon/<op-class-kebab>-<8-hex-prefix>` —
e.g. `tacon/add-file-bc247dc1`. Single source of truth in
`tacon/ops/_via_pr.py::via_pr_branch_name`. Diverges from the design
doc's `tacon-bot/<full-uuid>` sketch (rationale in `plans/via_pr.md`).

**Schema columns:** `events.pr_number INTEGER` and `events.pr_branch TEXT`,
both nullable. Direct-write events store NULL. Migration is
**fully idempotent** — fresh DBs are built straight at v2; v1 DBs
add the columns on next `open_db`.

---

## 4 · v0.2 candidates (what's still on the table)

In rough priority order. Four items already shipped (marked DONE).

### 4.1 — Finish `tacon resume` properly (HIGH) ✅ DONE in `bc247dc`

(Path-(a) flavor.) Each op's `args` dict includes everything needed
for reconstruction. If a future user asks for path-(b) (a content-
addressable blob store keyed by blob SHA so resume rehydrates without
`--content-from`), the hook is `tacon/cli.py::_reconstruct_op` and the
JSON contract in each op's `args` property.

### 4.2 — `--via-pr` mode for write ops (HIGH) ✅ DONE in 8 commits

See §-1 for the commit sequence. The eng-reviewed plan is in
`plans/via_pr.md`. Summary: 4 write ops + helpers + schema v2 +
CLI + resume + README + 1 live test.

### 4.3 — AddBranchProtection write mode (MEDIUM)

Today: read-only survey. Write mode needs:
- Admin-scoped token (most TA tokens don't have it)
- A way to express "desired protection" — likely a `BranchProtectionRule`
  dataclass with the typical fields (required_pull_request_reviews,
  required_approving_review_count, required_status_checks, etc.)
- `supports_rollback` decision: True (snapshot-and-restore the prior
  rule) or False (admin actions usually shouldn't auto-revert).
  **Recommend False** for safety; let the user re-run with the prior
  rule if they want.

Worth speccing with `/plan-eng-review` before coding — there's a real
shape decision around the BranchProtectionRule dataclass.

### 4.4 — Dashboard `--publish` to gh-pages (MEDIUM)

Today: prints "not yet wired" and exits 2 (`tacon/cli.py::dashboard`,
the `if publish:` branch). A clean implementation:
- Take `--publish <owner>/<repo>` (the dashboard target repo, NOT the
  classroom).
- Render to a tmp dir, then push to that repo's `gh-pages` branch via
  PyGithub OR shell out to `gh`. Design choice — talk to the user.
- Idempotent: each run replaces the gh-pages tip.

### 4.5 — PyPI publish (MEDIUM) ✅ PREP DONE in `7db9398`

Build + wheel inspection + console-script verification all green. The
user runs `twine upload` themselves. **Bumping `__version__` from
0.1.0 → 0.2.0** before they upload is the next pre-publish step (this
session's commits are v0.2 work but the version string still says
0.1.0 — intentional, mid-flight).

### 4.6 — Multi-classroom config (LOW)

One DB per classroom is fine for now. A `--classroom <id>` flag + a
`~/.tacon/classes.toml` index can be added in a v0.2.x point release if
any user actually has more than one.

### 4.7 — Live e2e: more ops (LOW) ✅ DONE in 5 commits

Live coverage now exists for every write op (direct + via-pr) plus the
read-only survey. Each test is self-cleaning via try/finally and uses
unique tacon-marked paths/branches/workflow-names so reruns don't
collide. The 7 read-only tests in `test_live_read.py` from v0.1.0 plus
the 11 write+rollback tests from v0.2 give 18 live tests in total.

### 4.8 — Schema v2 column for FixCIWorkflow rollback latency (LOW)

This was a *different* schema v2 from what we just shipped. The
already-landed migration added `pr_number`/`pr_branch`; the change in
this item would add a `previous_blob_sha` column on events so
FixCIWorkflow rollback can fetch the prior blob directly instead of
walking to the apply commit's parent (saves 1-2 API calls per rollback).
Only worth it if rollback latency turns out to be a real issue. **If
you do this, it's now a v3 migration** (bump `SCHEMA_VERSION` to 3 +
add a `_migrate_to_v3` after the existing one).

### 4.9 — Apply-method DRY (NEW LOW; tech debt from §4.2)

The four write ops' `apply()` methods all share the same boilerplate
shape: insert planned event → blocked check → confirm callback → try
direct-or-via-pr → catch BranchConflictError → catch GithubException →
update_event_status. ~100 lines × 4 ops = ~400 lines of similar code.
Could be DRYed via an `_apply_runner(self, db, gh, diff, confirm,
write_fn)` helper in `tacon/ops/__init__.py` or `_via_pr.py`. Skipped
this session because each op stays self-contained and the duplication
is concentrated in one spot per op. Refactor only if a 5th write op
gets added or if a bug forces touching all four at once.

---

## 5 · Known tech debt / gotchas

1. **Live tests fire on every pytest run** while `TACON_LIVE=1` is set
   in `.env`. Each run leaves an apply+revert pair of commits in the
   test repo's history (direct-write test) and a closed PR + deleted
   branch (via-pr test). Working tree stays clean.

2. ~~**`tacon resume` is partial.**~~ Done in `bc247dc`.

3. **`gh classroom` extension** may not be installed on every dev
   machine. `discover_via_gh_classroom` raises a clear error pointing
   to `--from-csv`. CSV fallback is the supported workflow until
   installed.

4. **sqlite-utils 3.39 upsert quirk** — silently no-ops when the table
   has `not_null=...` constraints. We use `.insert(..., pk=..., replace=True)`
   instead. Documented inline in `db.py`. If a write "succeeds" but
   the row isn't there, suspect this first.

5. **`tacon dashboard --publish` is stub-only.** §4.4.

6. **`AddCIWorkflow` imports `_NAME_RE` from `add_ci_workflow`** in
   `fix_ci_workflow`. Sibling-module private import; tolerable but
   could move to a shared `tacon/ops/_validation.py` if a third op
   needs the same regex.

7. **PyGithub 2.x deprecations** addressed in `527c3d6`. Watch for new
   ones if we depend on more PyGithub APIs.

8. **Python 3.13 in the venv but CI tests 3.10/3.11/3.12.** All works;
   if you add 3.13-only syntax (`type` statement, etc.) it'll break CI.

9. **PyGithub API asymmetry.** `repo.create_git_ref(ref="refs/heads/X")`
   takes the "refs/" prefix; `repo.get_git_ref(ref="heads/X")` does
   NOT. Documented in `_via_pr.py`'s module docstring. Mistakes here
   surface as 404s, not parse errors.

10. **PR-already-exists 422 messages vary.** `create_pull` puts the
    "A pull request already exists" marker either on top-level
    `message` or in `errors[].message`. `_is_pull_already_exists`
    handles both. If a future PyGithub version normalizes this, the
    second branch becomes dead code.

11. **`__version__` is still 0.1.0.** v0.2 is mid-flight; bump to
    0.2.0 only at publish time per §4.5.

12. **Apply-method duplication across 4 ops.** §4.9. Tolerable for now.

13. **Branch-delete permission soft-failure.** When `tacon rollback`
    closes a via-pr PR but the token can't delete the branch (rare —
    most TA tokens can), the rollback still counts as `rolled_back`
    with a note in `error_message`. The leftover branch is harmless but
    visible in GitHub's branch list. Not blocking.

---

## 6 · How to use gstack skills here

Project `CLAUDE.md` has skill routing. Heuristic: **skip the skill
ceremony for polish/debugging; invoke for substantial new work.**

| Situation | Skill | Why |
|---|---|---|
| Planning a non-trivial feature (e.g. §4.3, §4.4) | `/plan-eng-review` | Pressure-test architecture before coding. The §4.2 plan in `plans/via_pr.md` is the model. |
| Brainstorming op surface | `/office-hours` | Lower-friction than plan-eng-review. |
| Before pushing a feature | `/review` | Catches diff-level bugs/inconsistencies. |
| Committing + PRing | `/ship` | Safe commit + push flow. |
| Hunting a bug | `/investigate` | Root-cause first, fix second. |
| End of session | `/context-save` | Pairs with `/context-restore`; update *this* file too. |

If you're about to add a substantial new module (`--publish` in §4.4
is a good example: design choices around PyGithub vs `gh`, branch
handling, auth), invoke `/plan-eng-review` first and write the plan to
`plans/<feature>.md`. The reviewed plan is your decision log.

---

## 7 · Environment recap

- **Working dir:** `/home/tzun/repos/gstack-test/tacon/`
- **Git root:** `/home/tzun/repos/gstack-test/` (the `tacon/` subdir
  is the Python package + tests + pyproject + plans).
- **Remote:** `https://github.com/Tzun27/tacon.git` — `main` is the
  only branch; pushed through `16d168b` as of this handoff.
- **venv:** `/home/tzun/repos/gstack-test/tacon/.venv/` (Python 3.13.5).
  Activate with `source .venv/bin/activate` or call `.venv/bin/<tool>`
  directly. If pytest collection fails on `textual`, re-install dev
  extras: `.venv/bin/pip install -e ".[dev]"`.
- **Default DB path:** `~/.tacon/tacon.db` (overridable via `--db` or
  `TACON_HOME`). Live tests use a per-test `tmp_path` DB; they never
  touch the user's real DB.
- **Memory dir:** `/home/tzun/.claude/projects/-home-tzun-repos-gstack-test/memory/`
- **`.env`:** at `tacon/.env` (gitignored). Contains the user's GitHub
  PAT plus live-test scope config. **Never read or echo its contents.**
- **Plans dir:** `tacon/plans/` (tracked). Per-feature eng-review
  artifacts. `via_pr.md` is the only one as of this handoff.

---

## 8 · TL;DR for the impatient

1. Run `pytest -q --no-cov`, `ruff check .`, `mypy tacon` to confirm
   nothing broke since the handoff. Expect **242 unit tests passing**
   (+18 live if `TACON_LIVE=1`), ruff clean, mypy clean across **16
   source files**.
2. Ask the user **which v0.2 item from §4** they want next. Four
   already shipped (4.1, 4.2, 4.5, 4.7). The remaining MEDIUM items
   (§4.3 AddBranchProtection write mode, §4.4 dashboard `--publish`)
   both want a `/plan-eng-review` pass before coding.
3. Use `/plan-eng-review` for non-trivial new modules; write the plan
   to `plans/<feature>.md`. Otherwise just code.
4. Periodic commits — small, scoped, with clear `feat/fix/test/docs`
   prefixes. The git log so far is the model.
5. `/context-save` at the end of your session and update this file.
