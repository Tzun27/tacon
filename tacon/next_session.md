# Next session — pick up tacon mid-v0.2

You (the next agent) are continuing work on **tacon**, a TA workbench for
GitHub Classroom. v0.1.0 shipped + two v0.2 items shipped on top
(see §-1). The remaining HIGH-priority v0.2 work needs design input from
the user — that's why this session paused.

This file is your handoff. Read it top-to-bottom before doing anything else.

---

## -1 · What shipped after v0.1.0 (this session)

Two commits on top of `7c181e5` (the v0.1.0 handoff doc):

| Commit | What |
|---|---|
| `bc247dc` | **§4.1 done.** `tacon resume` finished. Reconstructs the op from `op_args_json`, replays apply on only the originally-failed repos, gets a fresh op_id, annotates the original failed events with `(resumed in op_id=Y)`. add-file/add-ci-workflow require `--content-from`; reject byte-length mismatches. fix-ci-workflow re-derives the bump-action transform from `transform_id`. delete-file and add-branch-protection need no extra flags. 8 new resume tests; 197 unit tests passing; coverage 94.47%. |
| `7db9398` | **§4.5 partial.** Smoke-tested PyPI publish prep: `python -m build` produces clean wheel + sdist, dashboard templates land in `tacon/dashboard/templates/`, console-script `tacon` resolves, `tacon dashboard --out` renders cleanly from a fresh-venv install. **Found+fixed:** `pip install 'tacon[tui]'` was being mangled by rich (it parsed `[tui]` as a malformed style tag and stripped it). Bracket now escaped. The user still does `twine upload` themselves — nothing pushed to PyPI. |

What's left for v0.2 — see §4. The remaining HIGH item (§4.2 `--via-pr`)
explicitly says to spec with `/plan-eng-review` first, so this session
stopped here.

---

## 0 · Orient yourself (5 min)

```bash
cd /home/tzun/repos/gstack-test/tacon
git status --short                # should be clean (or just .gitignored .env / .venv / dist)
git log --oneline | head -5       # confirm 7db9398 is the tip
.venv/bin/pytest -q --no-cov      # 197 unit tests pass in ~20s; +6 live tests if TACON_LIVE=1
.venv/bin/ruff check . && .venv/bin/mypy tacon   # both clean
```

Expected:
- `7db9398 fix(tacon): escape [tui] in TUI-missing install hint …` is the tip of `main`.
- 197 unit tests pass; 9 live tests pass **if the user's `.env` has
  `TACON_LIVE=1`** (it does as of v0.1.0). Live tests hit the real
  GitHub API.
- Coverage 94.47% (gate 90%). ruff + mypy clean across 15 source files.

> **Drift caught last session:** the venv had lost its dev extras
> (`textual`, `pytest-asyncio`, `types-pyyaml`). If pytest collection
> fails with `ModuleNotFoundError: No module named 'textual'`, run
> `.venv/bin/pip install -e ".[dev]"` and try again.

If any of that fails, **stop and investigate before continuing** — something
has drifted since this handoff was written.

> **HEADS-UP about the live tests.** The user's `.env` is configured to run
> live tests on every full `pytest` invocation. Each run does a real
> apply+rollback on `Netdb-NCKU/pre-test-hw-Tzun27`, leaving two commits
> in that repo's history (an apply commit and a revert commit) that **do
> not** get garbage-collected. The file tree is left clean. If the user
> doesn't want this, flip `TACON_LIVE=0` in `.env` (gitignored — safe to
> tell them how to edit it but **never read or echo its contents** since
> it has the token).

---

## 1 · What exists at v0.1.0 (don't rebuild)

### Code (tacon/)

```
tacon/
├── __init__.py                       __version__ = "0.1.0"
├── cli.py                            Typer app: sync, run, rollback, resume,
│                                     ui, dashboard, version
├── classroom.py                      gh classroom + CSV roster discovery
├── db.py                             SQLite: assignments, students, repos,
│                                     events, interactions, meta (schema v1)
├── github_client.py                  RateLimitedClient (Auth.Token) + 8-class
│                                     classify_error() + retry-after parsing
├── ops/
│   ├── __init__.py                   Op ABC + 6 dataclasses + lazy
│   │                                 pkgutil-based registry auto-discovery
│   ├── add_file.py                   AddFile (subclass-friendly: op_class_name
│   │                                 + default_revert_message class attrs)
│   ├── delete_file.py                DeleteFile (rollback restores from
│   │                                 git blob via repo.get_git_blob)
│   ├── add_ci_workflow.py            AddCIWorkflow (subclasses AddFile;
│   │                                 YAML validation + workflow-aware diff)
│   ├── fix_ci_workflow.py            FixCIWorkflow (transform-based patching;
│   │                                 rollback via apply commit's parent;
│   │                                 ships make_bump_action_transform)
│   └── add_branch_protection.py      Read-only survey (supports_rollback=False;
│                                     writes status='reported' events)
├── dashboard/
│   ├── __init__.py                   re-exports render
│   ├── render.py                     Jinja2 → static HTML; reads events +
│   │                                 repos + students; builds repos×ops grid
│   └── templates/
│       ├── base.html, index.html, op.html, repo.html
└── tui/
    ├── __init__.py                   re-exports TaconApp
    └── app.py                        Textual: ops pane + events pane;
                                      keybindings q/r/esc; last_status attr
                                      for tests
```

Five ops registered: `add-file`, `delete-file`, `add-ci-workflow`,
`fix-ci-workflow`, `add-branch-protection`. Auto-discovered on first
`list_ops()` / `get_op_class()` call — drop a new file in `tacon/ops/` and
it appears automatically (must call `register("name", Cls)` at the bottom).

### Tests (tests/, 198 passing)

- `tests/conftest.py` — `tmp_db`, `seed_repos`, `fake_repo`, `fake_gh`
- `tests/test_db.py` — 13 schema/upsert/event tests
- `tests/test_github_client.py` — 43 tests: classify_error matrix, retry
  loops, Retry-After/X-RateLimit-Reset parsing, token resolution paths
- `tests/test_classroom.py` — 11 CSV + gh shell-out tests
- `tests/test_cli.py` — 32 integration tests (mock RateLimitedClient at
  the `tacon.cli` boundary; cover every op, rollback, resume, _make_confirm
  state machine)
- `tests/test_dashboard.py` — 12 render + grid + CLI tests
- `tests/test_tui.py` — 6 Textual Pilot tests (`run_test()` + `pilot.press()`)
- `tests/ops/test_*.py` — 60 unit tests across the 5 ops + auto-discovery
- `tests/live/` — opt-in live e2e (see §3 below)

### CLI surface

```
tacon sync <classroom-id>
tacon sync --from-csv repos.csv
tacon run add-file --path X --content-from F [--apply --yes]
tacon run delete-file --path X [--apply --yes]
tacon run add-ci-workflow --workflow-name ci --content-from ci.yml [--apply --yes]
tacon run fix-ci-workflow --workflow-name ci \
    --bump-action actions/checkout@v3=actions/checkout@v4 [--apply --yes]
tacon run add-branch-protection [--branch main] [--apply --yes]
tacon rollback <op-id>
tacon resume <op-id>          # PARTIAL — see §4 tech debt
tacon ui                      # Textual TUI
tacon dashboard --out ./site  # static HTML
tacon dashboard --publish ... # NOT YET WIRED — exit 2
tacon version
```

### Tooling

- `pyproject.toml` — name `tacon`, version `0.1.0`. Deps: PyGithub≥2.1,
  typer≥0.12, rich≥13.7, sqlite-utils≥3.36, jinja2≥3.1, PyYAML≥6.0.
  Dev extras add textual, pytest-asyncio, types-PyYAML.
  Coverage gate `--cov-fail-under=90`. `asyncio_mode = "auto"`.
- `.github/workflows/ci.yml` — matrix on Python 3.10/3.11/3.12.
- `.venv/` — Python 3.13.5; everything installed.
- `.env` (gitignored) — live test config + GitHub token.
- `.env.example` — documented template; uses
  `Netdb-NCKU/pre-test-hw-Tzun27` as the worked example.

---

## 2 · What shipped THIS session (v0.0.1 → v0.1.0)

14 commits, summarized:

| Commit prefix | What |
|---|---|
| `e509157` | DeleteFile op + 12 tests (blob-restore on rollback) |
| `c9237be` | AddCIWorkflow op + 14 tests (YAML validation; refactored AddFile to use class attrs so subclasses plug in cleanly) |
| `6699ac9` | FixCIWorkflow op + 18 tests (transform callback; rollback via apply commit's parent) |
| `e1743fc` | AddBranchProtection op + 11 tests (read-only) |
| `be9a877` | Auto-discovery: `pkgutil.iter_modules` + lazy `_ensure_discovered()` flag — no more import-side-effects |
| `7f6e843` | CLI integration tests: cli.py 33% → 97% |
| `dc44e35` | github_client edge tests: 65% → 99% |
| `44da8b1` | Coverage gate 70 → 90 |
| `05a9179` | Static dashboard (Jinja2 PackageLoader; index/op/repo pages + style.css) |
| `fac7d08` | Textual TUI + bump to 0.1.0 |
| `4e25f41` | README updated to reflect v0.1.0 |
| `9c46c64` | Live e2e harness (opt-in, scope-guarded) |
| `5901f1a` | .env.example uses concrete `Netdb-NCKU/pre-test-hw-Tzun27` example |
| `527c3d6` | Auth.Token to silence PyGithub deprecation |

Coverage today: **95%** across 19 source files. cli.py at 96%, dashboard at
100%, tui at 99%, all 5 ops at 90+%. Project total LOC ~3.5K source + ~3.7K
test.

---

## 3 · Live e2e tests (the safety story)

Read this before touching anything that talks to real GitHub.

**Configuration is in `tacon/.env`** (gitignored). It currently contains:
- `TACON_GITHUB_TOKEN` — user's GitHub PAT (scoped to repo + their org)
- `TACON_TEST_ORG=Netdb-NCKU`
- `TACON_TEST_ASSIGNMENT_PREFIX=pre-test-hw`
- `TACON_TEST_REPO=Netdb-NCKU/pre-test-hw-Tzun27` (pinned to user's own repo)
- `TACON_LIVE=1` (live tests enabled — see warning above)

**Hard scope guard:** `tests/live/conftest.py::assert_in_scope(repo_full_name)`
raises `OutOfScopeError` (and aborts the test) if the repo isn't in
`TACON_TEST_ORG` or doesn't carry `TACON_TEST_ASSIGNMENT_PREFIX` in its
name. Every live test calls this before any API interaction. The
discovery fixtures double-check by also calling it on every result.

**The aiase2026 umbrella has multiple classrooms.** The user explicitly
asked us to never operate outside `pre-test-hw`. The scope guard enforces
this. **Do not loosen it.** If a future test needs broader scope, talk to
the user first.

**Live tests:**
- `test_live_read.py` (7 tests, read-only) — token auth, org visibility,
  repo discovery, single-repo metadata read, three scope-guard self-tests
- `test_live_apply_rollback.py` (2 tests, write+rollback) — full
  AddFile.plan → apply → verify → rollback → verify-clean cycle on a
  unique tacon-marked path (`.tacon-live-test/<random>.txt`); plus a
  blocked-when-present test against a real README. `try`/`finally`
  cleanup deletes any leftover even if mid-flight crashes.

To run only live tests: `.venv/bin/pytest tests/live/ -v --no-cov`.
To skip live during a quick run: prepend `TACON_LIVE=0`.

---

## 4 · v0.2 candidates (pick what the user wants)

In rough priority order based on what's actually missing or partial:

### 4.1 — Finish `tacon resume` properly (HIGH) ✅ DONE in `bc247dc`
Path (a) shipped. `tacon resume <op-id> [--content-from FILE] [--yes]`
reconstructs the op from `op_args_json`, replays apply on only the
originally-failed repos, gets a fresh op_id, annotates the original
failed events with `(resumed in op_id=Y)`. add-file/add-ci-workflow
require `--content-from` on resume; reject byte-length mismatches against
the stored `content_len`. fix-ci-workflow re-derives the bump-action
transform from `transform_id`. delete-file and add-branch-protection
need no extra flags.

If a future user asks for path (b) (content-addressable blob store
keyed by blob SHA so resume rehydrates without --content-from), the
hook is `tacon/cli.py::_reconstruct_op` and the JSON contract in each
op's `args` property.

### 4.2 — `--via-pr` mode for write ops (HIGH)
Right now every write goes direct-to-default-branch. Branch-protected
classrooms see per-repo failures with `error_class='permission'`.
Per the design doc, `--via-pr` would: create a branch in the student
repo, push the change there, open a PR. Apply still records the event;
rollback closes the PR (plus deletes the branch?). Big feature surface;
best to spec with `/plan-eng-review` before coding.

### 4.3 — AddBranchProtection write mode (MEDIUM)
Today: read-only survey. Write mode needs:
- Admin-scoped token (most TA tokens don't have it)
- A way to express "desired protection" (a `BranchProtectionRule`
  dataclass)
- supports_rollback could be True (snapshot-and-restore the prior rule)
  or False (admin actions usually shouldn't auto-revert)

Probably False for safety; let the user re-run with the prior rule if
they want.

### 4.4 — Dashboard `--publish` to gh-pages (MEDIUM)
Today: prints "not yet wired" and exits 2. A clean implementation:
- Take `--publish <owner>/<repo>` (the dashboard target repo, NOT the
  classroom)
- Render to a tmp dir, then push to that repo's `gh-pages` branch via
  PyGithub (or shell out to gh)
- Idempotent: each run replaces the gh-pages tip

### 4.5 — PyPI publish (MEDIUM) ✅ PREP DONE in `7db9398`
- `python -m build` produces a clean wheel + sdist.
- Wheel includes `tacon/dashboard/templates/{base,index,op,repo}.html`
  (verified with `unzip -l dist/tacon-0.1.0-py3-none-any.whl`).
- Console-script `tacon = "tacon.cli:app"` resolves after install;
  `tacon version` and `tacon dashboard --out` work from a fresh
  pip-installed wheel.
- TUI install hint was being mangled by rich's markup parser
  (`[tui]` was treated as a malformed style tag and stripped); fixed by
  escaping the bracket in the `tacon ui` ImportError branch.

**The user runs `twine upload` themselves.** Nothing was pushed to PyPI
this session. Did NOT add `dashboard` to optional-dependencies — the
dashboard command is core, not optional, so jinja2 stays required.

### 4.6 — Multi-classroom config (LOW)
One DB per classroom is fine for now. v0.2.x can add a `classrooms`
table + `--classroom <id>` flag if any user actually has more than one.

### 4.7 — Live e2e: more ops (LOW)
Today's live tests cover AddFile only. Add live tests for delete-file,
add-ci-workflow, fix-ci-workflow, add-branch-protection. Each follows
the same shape: scope guard → preflight → apply → verify → rollback →
verify clean.

### 4.8 — Schema v2 (LOW, for FixCIWorkflow rollback)
Today FixCIWorkflow's rollback fetches the prior content from the apply
commit's parent. That works but adds two API calls per repo to a
rollback. A `previous_blob_sha` column on events would let rollback
fetch the blob directly. Bump SCHEMA_VERSION 1 → 2 with an `add_column`
migration in `init_db`. Only worth it if rollback latency is a real
issue.

---

## 5 · Known tech debt / gotchas

1. **Live tests fire on every pytest run** while `TACON_LIVE=1` is set
   in `.env`. Each run leaves an apply+revert pair of commits in the
   test repo's history. Working tree stays clean.

2. ~~**`tacon resume` is partial.**~~ Done in `bc247dc` — see §4.1.

3. **`gh classroom` extension** may not be installed on every dev
   machine. `discover_via_gh_classroom` raises a clear error pointing to
   `--from-csv`. CSV fallback is the supported workflow until installed.

4. **sqlite-utils 3.39 upsert quirk** — silently no-ops when the table
   has `not_null=...` constraints. We use `.insert(..., pk=..., replace=True)`
   instead. Documented inline in `db.py`. If a write "succeeds" but the
   row isn't there, suspect this first.

5. **`--publish` is stub-only.** §4.4.

6. **Dashboard `op_id_label` printer arg** is unused-looking but is
   needed because `_print_apply_result` is called twice (once with label,
   once without — actually only once with label currently; could be
   simplified, low priority).

7. **AddCIWorkflow imports `_NAME_RE` from `add_ci_workflow`** in
   `fix_ci_workflow`. Sibling-module private import; tolerable but
   could move to a shared `tacon/ops/_validation.py` if a third op
   needs the same regex.

8. **PyGithub 2.x deprecation** addressed in `527c3d6`. Token now goes
   through `Auth.Token`. If we depend on more PyGithub APIs that get
   deprecated, watch for similar warnings during live test runs.

9. **Python 3.13 in the venv but CI tests 3.10/3.11/3.12.** All work; if
   you add 3.13-only syntax (`type` statement, etc.) it'll break CI.

---

## 6 · How to use gstack skills here

The user has gstack installed. Project `CLAUDE.md` has skill routing.

| Situation | Skill | Why |
|---|---|---|
| Resuming context (today's first message) | `/context-restore` | Loads prior plan + notes |
| Planning v0.2 scope | `/plan-eng-review` | Pressure-test architecture before coding |
| Brainstorming op surface | `/office-hours` | Lower-friction than plan-eng-review |
| Before committing | `/review` | Catches bugs/inconsistencies in the diff |
| Committing + PRing | `/ship` | Safe commit + push flow |
| Hunting a bug | `/investigate` | Root-cause first, fix second |
| Verifying behavior | `/qa` | End-to-end behavioral check |
| Saving progress at end of session | `/context-save` | Pairs with `/context-restore` |

**Heuristic:** if you're about to add a substantial new module
(e.g. starting `--via-pr`), invoke `/plan-eng-review` first. If you're
polishing or debugging, skip the ceremony.

---

## 7 · Environment recap

- **Working dir:** `/home/tzun/repos/gstack-test/tacon/`
- **Git root:** `/home/tzun/repos/gstack-test/` (the `tacon/` subdir is
  the Python package + tests + pyproject)
- **Remote:** `https://github.com/Tzun27/tacon.git` — `main` is the only
  branch; pushed.
- **venv:** `/home/tzun/repos/gstack-test/tacon/.venv/` (Python 3.13.5).
  Activate with `source .venv/bin/activate` or call `.venv/bin/<tool>`
  directly. If pytest collection 404s on `textual`, re-install dev
  extras: `.venv/bin/pip install -e ".[dev]"`.
- **Default DB path:** `~/.tacon/tacon.db` (overrideable via `--db` or
  `TACON_HOME`). Live tests use a per-test `tmp_path` DB; they never
  touch the user's real DB.
- **Memory dir:** `/home/tzun/.claude/projects/-home-tzun-repos-gstack-test/memory/`
- **`.env`:** at `tacon/.env` (gitignored). Contains the user's GitHub
  PAT plus live-test scope config. **Never read or echo its contents.**

---

## 8 · TL;DR for the impatient

1. Run `pytest -q --no-cov`, `ruff check .`, `mypy tacon` to confirm
   nothing broke since the handoff. Expect 197 unit / clean / clean
   (+9 live tests if `TACON_LIVE=1`).
2. Ask the user **which remaining v0.2 item from §4** they want next.
   Two HIGH/MEDIUM items already shipped (§4.1 resume, §4.5 PyPI prep).
   The remaining HIGH is §4.2 (`--via-pr`) which the handoff explicitly
   says to spec with `/plan-eng-review` first.
3. Use `/plan-eng-review` for non-trivial new modules; otherwise just
   code.
4. Periodic commits — small, scoped, with clear `feat/fix/test/docs`
   prefixes. The git log so far is the model.
5. `/context-save` at the end of your session and update this file.
