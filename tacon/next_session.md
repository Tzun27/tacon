# Next session — pick up tacon mid-v0.3

You (the next agent) are continuing work on **tacon**, a TA workbench for
GitHub Classroom. **v0.2.0 fully shipped** + the three v0.2.x follow-ups
(§4.6 / §4.8 / §4.9). **v0.3 in progress**: a local web GUI
(`tacon serve`) for TAs who don't want to use the CLI. The design doc
lives at `~/.gstack/projects/tacon/tzun-main-design-20260513-160839.md`
(produced via `/office-hours`, survived 2 rounds of adversarial review
at 8/10). **Steps 0-3 of the build order have shipped** — see -1
below. **Steps 4-11 remain.** See §9 for the v0.3 roadmap.

This file is your handoff. **Read it top-to-bottom before doing anything
else.** §-1 (what shipped this session) and §4 (what's left) are the
two parts you can't skip.

---

## -1 · What shipped after v0.1.0

**Thirty-five+ commits since the v0.1.0 handoff** (`7c181e5`):

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
| `8de18a7` | docs | Handoff doc reflects §4.7 done + 18 live tests total. |
| `44efd47` | §4.3 | **Plan for §4.3 AddBranchProtection write mode** at `plans/branch_protection_write.md`. Locked design (4/4 user-confirmed defaults): `--rule-from`/`--rule-template` only (no inline flags), supports_rollback=True via snapshot+restore, bundle tacon-default + strict-pr templates, live test pytest.skip on 403. |
| `7d96f09` | §4.3 | **Schema v3** — added `events.prior_state_json TEXT`. Idempotent migration in `_migrate_to_v3` mirroring v2. `insert_event`/`update_event_status` grew the kwarg. 5 new schema tests. |
| `54c6b0d` | §4.3 | **`BranchProtectionRule` dataclass + YAML loader** at `tacon/ops/_branch_protection_rule.py`. Frozen dataclass, strict validation (rejects unknown keys, wrong types, out-of-range counts), `to_edit_protection_kwargs()` maps to PyGithub's API. Empty `tacon/templates/protection/` package added; `_` prefix on the module excludes it from op auto-discovery. 27 new unit tests. |
| `81df2b1` | §4.3 | **Bundled templates `tacon-default` + `strict-pr`** at `tacon/templates/protection/*.yaml`. Verified to ship inside the wheel via `python -m build`. 4 new tests. |
| `06cae35` | §4.3 | **AddBranchProtection write mode** — op accepts optional `rule=BranchProtectionRule`. `plan()` renders a desired-state diff + idempotency block; `apply()` snapshots prior protection to `events.prior_state_json` then writes via `branch.edit_protection`; `rollback()` filters status='applied', drift-checks current vs applied, restores prior (or `remove_protection` if prior was null). `supports_rollback=True` at class level so the CLI passes through; survey op_ids yield empty rollback (clear "nothing to roll back" message). 15 new unit tests. |
| `245fa0d` | §4.3 | **CLI `--rule-from`/`--rule-template` flags** + write-mode resume. Mutually exclusive flags switch from survey to write mode; both-given/missing-file/bad-YAML/unknown-template all exit 2. Resume rehydrates the rule from `op_args.rule`. 7 new CLI tests covering the matrix. |
| `e931079` | §4.4 | **`tacon/dashboard/publish.py`** — `publish_to_gh_pages` helper. Atomic via the git-data API (blob → tree → commit → ref), so the branch never sits half-published. Fresh tree (no `base_tree`) per publish replaces the prior contents; commits chain to the existing tip when one exists, preserving an audit trail. Validates target_repo shape + skips dotfiles. Returns `PublishResult` with branch_status (`created`/`updated`) + a best-effort `pages_url`. |
| `5a7b289` | §4.4 | **CLI wiring for `--publish`** — `tacon dashboard --publish OWNER/REPO` (+ `--publish-branch`, `--publish-message`). Renders + publishes in one command. 19 new tests: input-validation, branch-create vs update, 404 fallback variants (UnknownObjectException AND generic GithubException(404)), custom branch, CLI flag forwarding, `PublishError` → exit 2, regression guard that no `RateLimitedClient` is constructed when `--publish` is omitted (render-only stays token-free). Removed the old "not yet wired" CLI test. |
| `a19063f` | §4.4, docs | **README + CLI help** — new "Dashboard --publish" section spells out user-page-vs-project-page distinction so it's obvious that pointing at a dedicated repo (`myorg/cs101-dashboard`) doesn't touch `<username>.github.io`. CLI help mirrors the warning. |
| `7a9c743` | §4.5 | **`__version__` 0.1.0 → 0.2.0** in `tacon/__init__.py` and `pyproject.toml`. Verified: fresh `python -m build` produces `tacon-0.2.0-py3-none-any.whl` + sdist; both contain `tacon/dashboard/publish.py` and the bundled protection templates. |
| `aa66ef7` | §4.9 | **Extract `_apply_runner`** — new module `tacon/ops/_apply_runner.py` with `WriteOutcome` dataclass + `run_per_repo_apply` helper. Factors the shared per-repo apply loop (insert planned event → blocked → confirm → try direct-or-via-pr → catch BranchConflictError → catch GithubException → update event + record result) out of the four content-write ops. Each op now supplies a `_direct_write` + `_apply_via_pr` returning `WriteOutcome` (or None to signal race-skip). AddFile refactored in this commit. AddBranchProtection deliberately stays self-contained (survey/write modes + `prior_state_json` snapshot + no via-pr don't fit the WriteOutcome shape). |
| `816bf00` | §4.9 | **DeleteFile uses `_apply_runner`** — same shape as the AddFile switch. |
| `472083e` | §4.9 | **FixCIWorkflow uses `_apply_runner`** — the transform-no-longer-applies race rides on the helper's `WriteOutcome | None` contract; race message stays "transform no longer applies (state changed since plan)" via the `race_skipped_message` kwarg. With this commit, all four content-write ops (AddFile, DeleteFile, AddCIWorkflow, FixCIWorkflow — the last inherits AddFile.apply unchanged) share the loop. |
| `9e2d9a4` | §4.8 | **Schema v4 + FixCIWorkflow rollback fast-path** — adds `events.previous_blob_sha` (idempotent `_migrate_to_v4` mirroring v2/v3). FixCIWorkflow apply records the pre-patch blob sha; rollback prefers it and fetches via `repo.get_git_blob` (1 call) instead of `get_commit` + `get_contents(ref=parent)` (2 calls + 404 risk). Falls back to the parent walk for pre-v4 events. `WriteOutcome.previous_blob_sha` is the new field; AddFile/DeleteFile leave it None. 6 new tests. |
| `7574710` | §4.6 | **Multi-classroom config** — new `tacon/classes.py` (load/save/resolve `~/.tacon/classes.toml`). CLI gains a global `--classroom <id>` option threaded through every existing command + a `tacon classroom list/add/set-default` subcommand group. DB-path precedence: `--db` > `--classroom` > default-in-classes.toml > legacy `~/.tacon/tacon.db`. Opt-in: missing classes.toml = exact legacy behavior. Adds conditional `tomli` dep for Python 3.10; TOML writer is hand-rolled. |
| `a878743` | §4.6 | **Tests for classes.py + CLI classroom integration** — 21 unit tests (parse paths, error paths, add/set-default semantics, resolve_db_path precedence) + 10 CLI integration tests (subcommand surface, --classroom flag wiring, --db beats --classroom, default auto-resolution). A `tacon_home` fixture isolates `TACON_HOME` per test so classes.toml writes never escape `tmp_path`. |
| `ec6d384` | v0.3 Step 0 | **`Op.arg_schema()` classmethod** — Pydantic model per Op describing `__init__` kwargs. Powers the v0.3 GUI's auto-generated forms via `.model_json_schema()` → React. AddFile/DeleteFile/AddCIWorkflow/FixCIWorkflow/AddBranchProtection all implement. FixCIWorkflow's runtime Callable transform is replaced in the schema by a `bump_action_from` + `bump_action_to` pair (same shape the CLI uses). AddBranchProtection embeds the rule shape as a nested optional model (None = survey). Adds `pydantic >= 2.0` to base deps. 10 new tests. |
| `b2c2318` | v0.3 Step 1 | **`tacon serve` skeleton** — new `tacon/server.py` with FastAPI app factory, host-header allowlist middleware (DNS-rebinding defense; localhost / 127.0.0.1 / [::1] only), free-port picker that walks 5734-5740, uvicorn launcher, and a `/healthz` route. New CLI: `tacon serve [--port N] [--host H] [--open/--no-open]`. New `tacon[gui]` extra (fastapi, uvicorn, sse-starlette, keyring). Dev extra mirrors it plus httpx for FastAPI testing. 13 new tests. |
| `0511eb2` | v0.3 Step 2 | **Op.apply accepts optional `op_id`** — refactor so the GUI server can pre-generate the UUID and return `{op_id}` to the client before the background apply task fires events. Each of the 5 ops + the runner now take `op_id: str \| None = None`, default = generate internally. 1 new test pins the passthrough. |
| `c188a46` | v0.3 Step 2 | **API endpoints + SSE feed** — new module `tacon/server_ops.py` owns the request-body → Op-instance bridge (per-op translation for FixCIWorkflow's transform callable and AddBranchProtection's nested rule). Server gains `GET /api/ops` (list + JSON Schema), `POST /api/ops/{name}/plan` (validates + runs plan(), returns Diff), `POST /api/ops/{name}/apply` (single-flight, returns op_id), `POST /api/ops/{op_id}/rollback`, and `GET /api/events?op_id=X&last_event_id=Y` (SSE with rowid cursor + 5s keep-alive). `_AppState` dataclass holds the in-flight slot; check-and-set is atomic under uvicorn's single event loop. Startup lifespan runs an orphan-sweep that rewrites stale `status='in-progress'` rows to `failed` (`error_class='server_restart'`). PyGithub work goes through `run_in_executor` so the SSE feed stays responsive. |
| `7b19251` | v0.3 Step 2 | **API + SSE test coverage** — 16 new tests in `test_server_api.py` covering GET /api/ops shape, plan/apply/rollback happy paths + 404/422/503/409, single-flight contention, lock release on completion, apply <500ms (proves background-task offload), SSE 3-repo final-state events, cursor resume w/o duplicates. Two production fixes folded in: host-header middleware moved to pure-ASGI (BaseHTTPMiddleware was buffering SSE bodies), and `api_plan` now opens its DB inside the executor (sqlite3 connections aren't thread-shareable). SSE tests spin up uvicorn in a daemon thread — httpx.ASGITransport buffers the entire body, and TestClient.stream() blocks on unbounded streams, so only a real wire harness works. 12 new tests in `test_server_ops.py` cover the request-body → Op-instance bridge layer in isolation. |
| `6cb8895` | v0.3 Step 2 | **Drop deprecation warning** — switch from `HTTP_422_UNPROCESSABLE_ENTITY` to `HTTP_422_UNPROCESSABLE_CONTENT` per starlette's RFC 9110 rename. Three call sites updated. |
| `8a52c31` | v0.3 Step 3 | **Vite + React + shadcn scaffold** — new SPA source tree at `tacon/web/`. Vite 8 + React 19 + TypeScript 6; shadcn/ui primitives (radix-nova preset) generated verbatim into `src/components/ui/` (button, input, textarea, switch, select, card, dialog, badge, progress, table); TanStack Query for the HTTP cache, `QueryClientProvider` wired in `main.tsx`. Boilerplate stripped to a minimal `App.tsx` ("tacon v0.3" heading). Tailwind v4 via `@tailwindcss/vite`; `@/*` path alias. Every dep pinned to an exact version (`package.json`) + `pnpm-lock.yaml` committed for deterministic CI. New `Makefile` with a `gui-dev` target (`pnpm install && pnpm build` in `tacon/web/`). |
| `4f16ac2` | v0.3 Step 3 | **Serve the built SPA at `/`** — `create_app()` mounts `tacon/web/dist/` as static files (`html=True`) when `dist/index.html` exists; when absent (fresh editable install) `GET /` returns a friendly page pointing at the build one-liner instead of a 404. Mount registered last so the catch-all doesn't shadow `/healthz` + `/api/*`. `_spa_dist_dir()` is a module-level resolver tests monkeypatch. |
| `fa8012e` | v0.3 Step 3 | **SPA mount test coverage** — new `tests/test_server_spa.py` (4 tests): dist-present serves `index.html` as `text/html`, dist-absent serves the build hint, `/healthz` + `/api/ops` survive the catch-all mount, real resolver targets `tacon/web/dist`. |

**All nine v0.2 items shipped** (§4.1-§4.9). **v0.3 in progress**: Steps
0 + 1 + 2 + 3 done, Steps 4-11 remaining (see §9 below).

---

## 0 · Orient yourself (5 min)

```bash
cd /home/tzun/repos/gstack-test/tacon
git status --short                   # clean (or just .gitignored .env / .venv / dist / tacon-dashboard)
git log --oneline | head -5          # tip should be the latest handoff-doc commit
.venv/bin/pytest -q --no-cov --ignore=tests/live   # 411 unit tests pass in ~40s
.venv/bin/ruff check . && .venv/bin/mypy tacon     # both clean
```

To build the GUI bundle (Step 3 onward) you also need `node` (≥20) and
`pnpm` (≥9). Build it once with `make gui-dev` (or `cd tacon/web &&
pnpm install && pnpm build`); without that, `tacon serve` shows a
build-hint page at `/` instead of the SPA.

Expected:
- The tip of `main` is the latest handoff-doc commit (this one). The
  Step 0-3 v0.3 commits sit above the v0.2.x commits. Most of the
  v0.2.x work is already pushed to `origin/main`; the v0.3 commits +
  this handoff are unpushed at handoff time.
- **411 unit tests pass** (+4 since the Step 2 handoff: 4 SPA
  static-file mount tests in `tests/test_server_spa.py`). 18 live unit
  tests + 1 live skip-on-403 still pass if `TACON_LIVE=1`.
- Coverage stays above 90% (gate; sits at ~94%). ruff + mypy clean
  across **24 source files** — `tacon/server.py` grew (SPA mount) but
  no new Python source module; the SPA lives under `tacon/web/` as a
  separate JS/TS tree, not Python source.

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
├── __init__.py                       __version__ = "0.2.0"
├── cli.py                            Typer app: sync, run (+ --via-pr),
│                                     rollback, resume (+ --content-from),
│                                     ui, dashboard, classroom (NEW),
│                                     version. Every command takes
│                                     --classroom <id> alongside --db.
├── classes.py                        Multi-classroom config (NEW §4.6).
│                                     Read/write ~/.tacon/classes.toml,
│                                     resolve_db_path() with precedence
│                                     --db > --classroom > default > legacy.
├── classroom.py                      gh classroom + CSV roster discovery
│                                     (note: different from classes.py —
│                                     this one is for student-repo discovery)
├── server.py                         FastAPI app (`tacon serve`).
│                                     Step 1 shipped the skeleton; Step 2
│                                     added GET /api/ops, POST plan/apply,
│                                     POST rollback, GET /api/events (SSE);
│                                     Step 3 mounts the built SPA at /
│                                     (_mount_spa + _spa_dist_dir; static
│                                     files when tacon/web/dist exists,
│                                     else a build-hint page).
│                                     _AppState dataclass holds the
│                                     in-flight slot + GH factory + DB
│                                     path. Host-allowlist is pure-ASGI
│                                     middleware so SSE chunks aren't
│                                     buffered. PyGithub work goes
│                                     through run_in_executor.
├── server_ops.py                     Request-body → Op-instance bridge.
│                                     Per-op translation for
│                                     FixCIWorkflow's transform callable
│                                     and AddBranchProtection's nested
│                                     rule dataclass. Outside server.py
│                                     so the construction logic stays
│                                     unit-testable without FastAPI in
│                                     the import graph.
├── db.py                             SQLite: assignments, students, repos,
│                                     events, interactions, meta
│                                     SCHEMA_VERSION = 4
│                                     v2 added events.pr_number/pr_branch
│                                     v3 added events.prior_state_json
│                                     (snapshot for AddBranchProtection rollback)
│                                     v4 added events.previous_blob_sha
│                                     (FixCIWorkflow rollback fast-path)
│                                     Idempotent _migrate_to_v2/3/4
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
│   ├── _apply_runner.py              Shared per-repo apply loop (NEW §4.9).
│   │                                 Exports: WriteOutcome dataclass,
│   │                                 run_per_repo_apply(...). Each
│   │                                 content-write op supplies a direct_write
│   │                                 + via_pr_write callable returning
│   │                                 WriteOutcome (or None for race-skip).
│   │                                 WriteOutcome.previous_blob_sha (NEW §4.8)
│   │                                 is threaded through to events.
│   │                                 The `_` prefix excludes from
│   │                                 auto-discovery.
│   ├── add_file.py                   AddFile (supports_via_pr=True). apply()
│   │                                 delegates to run_per_repo_apply via
│   │                                 _direct_write + _apply_via_pr (both
│   │                                 return WriteOutcome); _rollback_via_pr
│   │                                 unchanged; _push_file accepts branch=.
│   ├── delete_file.py                DeleteFile (supports_via_pr=True). Same
│   │                                 shape as AddFile: helper-based apply,
│   │                                 _apply_via_pr returns WriteOutcome,
│   │                                 _rollback_via_pr handles via-pr undo.
│   ├── add_ci_workflow.py            AddCIWorkflow (subclasses AddFile;
│   │                                 inherits helper-based apply wholesale;
│   │                                 just forwards via_pr through __init__).
│   ├── fix_ci_workflow.py            FixCIWorkflow (supports_via_pr=True).
│   │                                 Helper-based apply; _direct_write +
│   │                                 _apply_via_pr return WriteOutcome | None
│   │                                 (None = race: transform no longer
│   │                                 applies). Race message is configured via
│   │                                 the helper's race_skipped_message kwarg.
│   │                                 _patch now also returns the pre-patch
│   │                                 blob sha → events.previous_blob_sha
│   │                                 → rollback fast path (§4.8).
│   ├── add_branch_protection.py      Survey + write modes
│   │                                 (supports_rollback=True at class level,
│   │                                  rollback() filters status='applied' so
│   │                                  survey ops naturally yield empty;
│   │                                  supports_via_pr=False — repo-level config).
│   └── _branch_protection_rule.py    BranchProtectionRule dataclass + YAML
│                                     loader + bundled-template resolver.
│                                     `_` prefix excludes from auto-discovery.
├── templates/                        Bundled YAML rule templates (read via
│   │                                 importlib.resources; ship inside the wheel)
│   └── protection/
│       ├── tacon-default.yaml        1 review, dismiss-stale, no req. checks
│       └── strict-pr.yaml            2 reviews, enforce admins, linear history
├── dashboard/
│   ├── __init__.py                   re-exports render + publish_to_gh_pages
│   ├── render.py                     Jinja2 → static HTML
│   ├── publish.py                    `publish_to_gh_pages` (NEW v0.2 §4.4).
│   │                                 Atomic git-data API publish: blob → tree
│   │                                 → commit → ref. Fresh tree per publish
│   │                                 (no base_tree) so stale files don't
│   │                                 linger; commits chain to existing tip.
│   └── templates/
│       ├── base.html, index.html, op.html, repo.html
├── tui/
│   ├── __init__.py                   re-exports TaconApp
│   └── app.py                        Textual TUI
└── web/                              v0.3 GUI SPA source (NEW Step 3).
    │                                 Vite 8 + React 19 + TS 6 + shadcn/ui
    │                                 (radix-nova) + TanStack Query. NOT
    │                                 Python — a separate pnpm project.
    ├── src/App.tsx                   minimal "tacon v0.3" placeholder
    ├── src/components/ui/*.tsx       10 shadcn primitives (committed)
    ├── package.json                  exact-pinned deps + pnpm-lock.yaml
    └── dist/                         `pnpm build` output (gitignored;
                                      server.py serves it at /). Build
                                      with `make gui-dev`.
```

Five ops registered: `add-file`, `delete-file`, `add-ci-workflow`,
`fix-ci-workflow`, `add-branch-protection`. Auto-discovered (drop a new
file in `tacon/ops/` and call `register("name", Cls)` at the bottom).
**Modules starting with `_` are explicitly skipped** — that's why
`_via_pr.py` is helpers, not an op.

### Tests (tests/, 411 unit + 18 live + 1 skip-on-403 live)

- `tests/conftest.py` — `tmp_db`, `seed_repos`, `fake_repo`, `fake_gh`
- `tests/test_db.py` — 23 tests (5 schema-v2 + 5 schema-v3:
  fresh-build cols, v1→v2 + v2→v3 in-place migrations, re-run
  idempotency on each version, partial-state recovery, pr/prior-state
  field round-trips)
- `tests/test_github_client.py` — 43 tests
- `tests/test_classroom.py` — 11 CSV + gh shell-out tests
- `tests/test_cli.py` — 50 integration tests (via-pr CLI flag, resume of
  via-pr ops, add-branch-protection --via-pr rejection, +7 new for
  --rule-from / --rule-template / write-mode resume in §4.3)
- `tests/test_dashboard.py` — 12 tests
- `tests/test_dashboard_publish.py` — **19 tests (NEW v0.2 §4.4)** — input
  validation, branch-create vs update, 404 fallback variants, custom
  branch, CLI flag forwarding, error exit codes, no-publish-flag
  regression guard
- `tests/test_tui.py` — 6 Textual Pilot tests
- `tests/ops/test_via_pr.py` — 18 helper unit tests (v0.2 §4.2)
- `tests/ops/test_add_file.py` — 20 tests
- `tests/ops/test_delete_file.py` — 17 tests
- `tests/ops/test_add_ci_workflow.py` — 16 tests
- `tests/ops/test_fix_ci_workflow.py` — 23 tests
- `tests/ops/test_add_branch_protection.py` — **26 tests** (15 new for write mode + drift + describe)
- `tests/ops/test_branch_protection_rule.py` — **31 tests (NEW)** (rule validation, YAML, bundled templates)
- `tests/ops/test_registry_discovery.py` — unchanged
- `tests/test_server.py` — **11 tests** (Step 1: /healthz + host-header
  allowlist + port-picker)
- `tests/test_server_ops.py` — **12 tests** (Step 2: request-body →
  Op-instance bridge — every op's construction path including
  FixCIWorkflow transform callable + AddBranchProtection nested rule
  + WorkflowValidationError-as-422 surfacing)
- `tests/test_server_api.py` — **16 tests** (Step 2: GET /api/ops shape +
  flags, POST plan happy/404/422/503, POST apply op_id + <500ms +
  409 single-flight + lock release, POST rollback dispatch + 404, SSE
  feed delivers 3 final-state events + cursor resumes from N+1 without
  duplicates). SSE tests use uvicorn-in-a-thread (ASGITransport
  buffers SSE bodies, TestClient.stream() hangs on unbounded streams).
- `tests/test_server_spa.py` — **4 tests (NEW Step 3)** — GET / serves
  the built `index.html` as `text/html` when `dist/` exists,
  build-hint page when it's absent, `/healthz` + `/api/ops` survive
  the catch-all SPA mount, real `_spa_dist_dir()` targets
  `tacon/web/dist`. Dist resolver is monkeypatched per-test.
- `tests/live/` — opt-in live e2e (18 unit + 1 skip-on-403):
  - `test_live_read.py` (7 tests, read-only)
  - `test_live_apply_rollback.py` (2 tests, AddFile direct write+rollback)
  - `test_live_via_pr.py` (1 test, AddFile via-pr round-trip)
  - `test_live_delete_file.py` (2 tests, DeleteFile direct + blocked-when-absent)
  - `test_live_delete_file_via_pr.py` (1 test, DeleteFile via-pr round-trip)
  - `test_live_add_ci_workflow.py` (2 tests, AddCIWorkflow direct + via-pr)
  - `test_live_fix_ci_workflow.py` (2 tests, FixCIWorkflow direct + via-pr)
  - `test_live_add_branch_protection.py` (1 test, read-only survey)
  - `test_live_branch_protection_write.py` (**1 test, NEW** — write+rollback;
    skip-on-403 since most TA tokens lack admin scope)

### CLI surface (current)

Every non-classroom command also accepts `--classroom <id>` as a peer of
`--db` (omitted below for brevity).

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
                                # default: read-only survey
tacon run add-branch-protection --rule-from RULE.yaml [--apply --yes]
                                # write mode: apply rule from file
tacon run add-branch-protection --rule-template tacon-default [--apply --yes]
                                # write mode: bundled (tacon-default | strict-pr)
                                # --via-pr is REJECTED here (repo-level config)
tacon rollback <op-id>          # auto-detects via-pr events from pr_number
tacon resume <op-id> [--content-from FILE] [--yes]
                                # auto-detects via-pr from op_args.via_pr
tacon ui                        # Textual TUI
tacon dashboard --out ./site    # static HTML
tacon dashboard --publish OWNER/REPO [--publish-branch B] [--publish-message M]
                                # render + push to gh-pages on a dedicated repo
tacon classroom list            # multi-classroom: NEW §4.6
tacon classroom add <id> --db PATH [--description "..."] [--default]
tacon classroom set-default <id>
tacon version
```

### Tooling

- `pyproject.toml` — name `tacon`, version `0.2.0` (bumped at the end
  of v0.2 work, ready for `twine upload`). Adds a conditional
  `tomli >= 2.0; python_version < '3.11'` for §4.6's TOML reader on
  Python 3.10. Coverage gate `--cov-fail-under=90`.
  `asyncio_mode = "auto"`.
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

**Live tests** (9 files; 18 unit + 1 skip-on-403):
- `test_live_read.py` (7 tests, read-only) — token auth, org visibility,
  repo discovery, single-repo metadata read, three scope-guard self-tests.
- `test_live_apply_rollback.py` (2 tests) — AddFile direct-write
  apply→verify→rollback→verify-clean on a tacon-marked path; plus
  blocked-when-present against a real README.
- `test_live_via_pr.py` (1 test) — AddFile(via_pr=True) end-to-end:
  branch created at default-branch HEAD, file written ON the branch (NOT
  default), PR opened, rollback closes the PR + deletes the branch.
- `test_live_delete_file.py` (2 tests) — DeleteFile direct-write
  apply→rollback (rollback re-creates the file with the original blob
  sha); plus blocked-when-path-absent.
- `test_live_delete_file_via_pr.py` (1 test) — DeleteFile via-pr:
  default branch keeps the file, the tacon branch loses it; rollback
  closes PR + deletes branch + leaves default untouched.
- `test_live_add_ci_workflow.py` (2 tests) — AddCIWorkflow direct +
  via-pr against a unique tacon-marked workflow filename.
- `test_live_fix_ci_workflow.py` (2 tests) — FixCIWorkflow direct +
  via-pr; seeds checkout@v3, bumps to @v4, verifies + rolls back.
- `test_live_add_branch_protection.py` (1 test) — read-only survey;
  asserts the per-repo summary matches a known shape and the event
  records as `reported`.
- `test_live_branch_protection_write.py` (1 test, **skip-on-403**) —
  AddBranchProtection write mode end-to-end: snapshot prior state,
  apply tacon-default rule, verify, rollback, verify prior restored.
  Skips with a clear message when the token lacks admin scope (the
  realistic TA case — verified to skip cleanly on the user's token).

Every write+rollback test uses `try/finally` cleanup so the test repo
returns to a clean state even on mid-flight crashes.

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
**fully idempotent** — fresh DBs are built straight at the current
SCHEMA_VERSION (4); v1/v2/v3 DBs add the columns on next `open_db` via
`_migrate_to_v2` + `_migrate_to_v3` + `_migrate_to_v4` respectively.
v3 also added `events.prior_state_json TEXT` for AddBranchProtection
write-mode rollback (see §4.3). v4 added `events.previous_blob_sha TEXT`
for FixCIWorkflow rollback fast-path (see §4.8).

---

## 4 · v0.2 candidates (what's still on the table)

In rough priority order. **Six items shipped** (4.1, 4.2, 4.3, 4.4, 4.5,
4.7 marked DONE); only LOW/speculative items remain.

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

### 4.3 — AddBranchProtection write mode (MEDIUM) ✅ DONE in 6 commits

Plan locked in `plans/branch_protection_write.md`. Summary of what
shipped: schema v3 (`prior_state_json` column), `BranchProtectionRule`
dataclass + YAML loader at `tacon/ops/_branch_protection_rule.py`,
bundled `tacon-default` + `strict-pr` templates, write-mode op
(`AddBranchProtection(rule=...)`), CLI `--rule-from` / `--rule-template`
flags, write-mode resume, live test (skip-on-403).

Future extensions beyond v0.2:
- **Inline flag overrides** (`--required-approvals 2`) — not shipped.
  If a TA wants this, `tacon/cli.py::run` is the hook; build a
  `BranchProtectionRule` from the flag args before falling through to
  the file/template logic.
- **More bundled templates** (e.g. `status-checks-only`) — drop new
  YAML in `tacon/templates/protection/`; auto-discovered.
- **Org-level Repository Rule Sets** — separate API surface; would be
  a new op (`add-org-ruleset`?), not a write-mode of this one.

### 4.4 — Dashboard `--publish` to gh-pages (MEDIUM) ✅ DONE in 3 commits

Shipped: `tacon/dashboard/publish.py::publish_to_gh_pages` (atomic via
the git-data API — blob → tree → commit → ref; fresh tree per publish
so stale files don't linger; commits chain to existing tip preserving
audit trail). CLI: `tacon dashboard --publish OWNER/REPO`, plus
`--publish-branch` and `--publish-message`. README and CLI help warn
about the user-page vs project-page distinction (don't aim at
`<username>/<username>.github.io`). Chose PyGithub over `gh` shell-out
for consistency with the rest of the codebase. 19 unit tests; no live
test (would require a sacrificial dashboard repo + extra scope guard —
deferred until anyone actually wants live coverage of this).

Future extensions:
- **Live e2e** — would need a dedicated test target repo (the existing
  `pre-test-hw-Tzun27` test repo isn't appropriate as a Pages target).
  Add `TACON_TEST_PAGES_REPO` to `.env.example` if/when this happens.
- **Custom domain (`CNAME`)** — currently `pages_url` is a best-effort
  guess at `https://<owner>.github.io/<repo>/`. If the user wants a
  custom domain we could let them pass `--publish-cname` and write the
  `CNAME` file into the published tree.
- **`.nojekyll`** — none of our files start with `_`, so we don't need
  it today. If a future template adds underscore-prefixed assets, drop
  a `.nojekyll` into `_collect_site_files`'s output.

### 4.5 — PyPI publish (MEDIUM) ✅ DONE (`7a9c743`)

Version bumped to 0.2.0. `python -m build` produces clean
`tacon-0.2.0-py3-none-any.whl` + `tacon-0.2.0.tar.gz` with all v0.2
modules and templates included. **The user runs `twine upload` themselves**
when ready to publish to PyPI.

### 4.6 — Multi-classroom config (LOW) ✅ DONE in 2 commits

Shipped: `tacon/classes.py` (load/save/resolve `~/.tacon/classes.toml`).
Global `--classroom <id>` flag on every command. New
`tacon classroom list/add/set-default` subcommands. Backwards compatible:
absent `classes.toml` = exact legacy behavior. DB-path precedence is
documented in `tacon/classes.py::resolve_db_path` and the README.

Commits: `7574710` (implementation), `a878743` (31 tests). Adds a
conditional `tomli` dep for Python 3.10 (tomllib is stdlib only on
3.11+); writer is hand-rolled since the file is tiny.

Future extensions:
- **`tacon classroom remove <id>`** — currently the user edits
  `classes.toml` by hand to drop an entry. Easy to add when a user
  actually wants the CLI helper.
- **Per-classroom defaults beyond `db_path`** — e.g. `default_branch`
  or a roster CSV path could move into the TOML if classrooms diverge
  on those settings.

### 4.7 — Live e2e: more ops (LOW) ✅ DONE in 5 commits

Live coverage now exists for every write op (direct + via-pr) plus the
read-only survey. Each test is self-cleaning via try/finally and uses
unique tacon-marked paths/branches/workflow-names so reruns don't
collide. The 7 read-only tests in `test_live_read.py` from v0.1.0 plus
the 11 write+rollback tests from v0.2 give 18 live tests in total.

### 4.8 — Schema column for FixCIWorkflow rollback latency (LOW) ✅ DONE in 1 commit

Shipped: schema v4 added `events.previous_blob_sha` (idempotent
`_migrate_to_v4` mirroring v2/v3). FixCIWorkflow apply records the
pre-patch blob sha; rollback fetches it via `repo.get_git_blob` (1
API call) instead of walking to the apply commit's parent (2 API
calls plus a 404 risk if the file didn't exist before). Pre-v4 events
(where `previous_blob_sha` is NULL because the column didn't exist at
write time) keep the old parent-walk path for backwards compat.
`WriteOutcome.previous_blob_sha` is the field the apply runner threads
through; AddFile/DeleteFile leave it None.

Commit: `9e2d9a4`. 6 new tests (5 DB schema + 1 fallback regression).

Future extensions:
- **Other ops that could benefit** — none of the v0.2 ops do
  similar parent-walks. If a future op needs a prior-blob snapshot,
  it can fill `WriteOutcome.previous_blob_sha` the same way.
- **Backfill for pre-v4 events** — not implemented; the slow-path
  fallback handles them just fine. Backfilling would require walking
  every apply commit's parent in a one-time migration job, not worth
  the complexity for an audit-trail optimization.

### 4.9 — Apply-method DRY (LOW) ✅ DONE in 3 commits

Shipped: `tacon/ops/_apply_runner.py` with `WriteOutcome` dataclass +
`run_per_repo_apply(*, op_class_name, op_args, via_pr, db, gh, diff,
confirm, direct_write, via_pr_write=None, race_skipped_message=...)`.
The helper handles the full per-repo lifecycle (event insert/update,
blocked, declined, BranchConflictError, GithubException, race-skip).
Each content-write op now exposes a thin `_direct_write` and
`_apply_via_pr` returning `WriteOutcome` (or `None` for race-skip);
apply() is a single call to the helper.

Commits: `aa66ef7` (helper module + AddFile switch), `816bf00`
(DeleteFile), `472083e` (FixCIWorkflow). AddCIWorkflow inherits
AddFile.apply unchanged.

AddBranchProtection deliberately stays self-contained: it has a
survey/write mode split, no via-pr support, no commit/blob fields, and
a `prior_state_json` snapshot — a poor fit for the WriteOutcome shape.
If a future op needs the prior_state-snapshot pattern, generalize the
helper at that point rather than now.

Future extensions:
- **Race-skip with separate event vs result messages** — the original
  FixCIWorkflow code had slightly different strings for the event
  (`"transform no longer applies (state changed since plan)"`) vs the
  RepoApplyResult (`"state changed since plan"`). The helper unifies
  them on the longer string. If a future op needs distinct messages,
  add a `race_result_message` kwarg paralleling `race_skipped_message`.

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

5. ~~**`tacon dashboard --publish` is stub-only.**~~ Done in `5a7b289`.

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

11. ~~**`__version__` is still 0.1.0.**~~ Bumped to 0.2.0 in `7a9c743`.

12. ~~**Apply-method duplication across 4 ops.**~~ Resolved by the §4.9
    refactor (commits `aa66ef7`, `816bf00`, `472083e`). All four
    content-write ops now share `tacon/ops/_apply_runner.run_per_repo_apply`.

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
| Planning a non-trivial feature (e.g. §4.4) | `/plan-eng-review` | Pressure-test architecture before coding. The §4.2 plan in `plans/via_pr.md` and the §4.3 plan in `plans/branch_protection_write.md` are the models. |
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
- **Git root:** `/home/tzun/repos/gstack-test/` (the `tacon/` subdir is
  the Python package + tests + pyproject + plans).
- **Remote:** `https://github.com/Tzun27/tacon.git` — `main` is the
  only branch. The §4.9 refactor trio (`aa66ef7`, `816bf00`, `472083e`)
  and the §4.9 handoff (`a39230a`) are pushed. The §4.8 + §4.6 commits
  (`9e2d9a4`, `7574710`, `a878743`) and this updated handoff still
  need a `git push origin main` from the user (Claude Code's permission
  default blocks pushes to `main` even on user request).
- **venv:** `/home/tzun/repos/gstack-test/tacon/.venv/` (Python 3.13.5).
  Activate with `source .venv/bin/activate` or call `.venv/bin/<tool>`
  directly. If pytest collection fails on `textual`, re-install dev
  extras: `.venv/bin/pip install -e ".[dev]"`.
- **Default DB path:** `~/.tacon/tacon.db` (overridable via `--db` or
  `TACON_HOME`). Live tests use a per-test `tmp_path` DB; they never
  touch the user's real DB.
- **Memory dir:** `/home/tzun/.claude/projects/-home-tzun-repos-gstack-test/memory/`
  (the parent project dir is `gstack-test`, even though the package source
  is in the `tacon/` subdir — Claude Code keys the memory dir off the
  outermost dir Claude was launched from).
- **`.env`:** at `tacon/.env` (gitignored). Contains the user's GitHub
  PAT plus live-test scope config. **Never read or echo its contents.**
- **Plans dir:** `tacon/plans/` (tracked). Per-feature eng-review
  artifacts. Two now: `via_pr.md` (§4.2) and
  `branch_protection_write.md` (§4.3).

---

## 9 · v0.3 GUI roadmap (where you pick up)

**Design doc:** `~/.gstack/projects/tacon/tzun-main-design-20260513-160839.md`
— survived 2 rounds of adversarial review at 8/10. Read this end-to-end
before touching code. Key sections:

- **"Op Schema → Form Bridge"** explains how `Op.arg_schema()` (already
  implemented) feeds JSON Schema to the React form generator. Step 5.5
  builds the renderer that consumes it.
- **"Sub-scope for v0.3"** lists the 7 in-scope feature areas.
- **"Out of scope for v0.3"** lists what to push to v0.3.1/v0.4. Audit-
  trail browser deferred. AI-assist deferred. Dark mode toggle deferred.
- **"Reviewer Concerns"** — the one persisted concern: the "Update CI
  workflow" card's mode-toggle default needs TA usability validation
  before split-into-two-cards decision.

**Build order — Steps 0-3 done; 4-11 remain.** Estimated 70 hours total
(see Next Steps in the design doc for the per-step breakdown).

| Step | Status | Title | Estimated |
|---|---|---|---|
| 0 | ✅ DONE (`ec6d384`) | Op.arg_schema() on all 5 ops | 2.5h |
| 1 | ✅ DONE (`b2c2318`) + ✅ live-validated | tacon serve skeleton (FastAPI + CLI) | 4h |
| 2 | ✅ DONE (`0511eb2`+`c188a46`+`7b19251`+`6cb8895`) | API: op listing + plan/apply/rollback + SSE | 10h |
| 3 | ✅ DONE (`8a52c31`+`4f16ac2`+`fa8012e`) + ✅ live-validated | Vite + React + shadcn scaffold | 3h |
| 4 | NEXT | Settings page first | 5h |
| 5 | | Verb-card home screen | 3h |
| 5.5 | | Schema-driven form renderer | 6h |
| 6 | | One op end-to-end: AddFile spine | 24h |
| 7 | | Rest of ops onto the spine | 6h |
| 8 | | Past ops list + rollback UX | 4h |
| 9 | | Polish (loading/empty/error states, theme) | 3h |
| 10 | | CI/CD: build SPA in GitHub Actions | 3h |
| 11 | | Release v0.3.0 | 1h |

**Step 1 was live-validated** (not just unit-tested) — booted
`tacon serve --no-open --port 5734` and exercised: `/healthz` returns 200
+ correct version; `Host: localhost`, `Host: localhost:5734`,
`Host: 127.0.0.1` all 200; `Host: evil.example.com` and
`Host: 192.168.1.10` both 403 with named-host detail message; `/`,
`/does-not-exist`, `/docs`, `/openapi.json` all 404 (the auto docs are
intentionally disabled); second-server-same-port exits 2 with a clear
*"already in use"* message; second-server-auto-pick falls through to
port 5735. No surprises — the unit tests had it right.

**Step 3 update on `GET /`:** the Step-1 skeleton had `GET /` returning
404 (no SPA mounted). Step 3 fixed that — `create_app()` now mounts
`tacon/web/dist/` at `/`. When the bundle is built (`make gui-dev`) `/`
serves `index.html` (200, `text/html`) and `/assets/*` serve the JS/CSS;
when it's absent `/` returns a friendly build-hint page (still 200, not
404). Live-validated: booted `tacon serve --no-open --port 5734` with a
fresh `pnpm build` — `/` → 200 `index.html`, `/assets/*.js` → 200
`text/javascript`, `/healthz` + `/api/ops` still resolve (mount doesn't
shadow them), `Host: evil.example.com` → 403.

**Step 2 shipped over 4 commits** (`0511eb2`+`c188a46`+`7b19251`+`6cb8895`)
and covers everything the design doc spelled out:

- `GET /api/ops` — list of `{name, op_class, arg_schema (JSON Schema),
  supports_via_pr, supports_rollback}` for each registered op. Works on
  a bare `create_app()` (no DB/token needed) so the SPA can render the
  card list even before settings page is filled in.
- `POST /api/ops/{name}/plan` — Pydantic-validates the body against
  `arg_schema()`, constructs the op via the `tacon/server_ops.py`
  bridge, runs `plan()` in a thread executor, returns the serialized
  Diff.
- `POST /api/ops/{name}/apply` — pre-generates op_id, single-flight
  lock (`state.in_flight is not None`; atomic check-and-set under
  uvicorn's single event loop), schedules a background task that runs
  `plan()`-then-`apply()` in the executor, returns `{op_id, phase, op_name}`
  immediately. 409 with the in-flight op_id on contention.
- `POST /api/ops/{op_id}/rollback` — looks up `op_class` from the
  events table, dispatches `cls.rollback(...)` in a background task.
  404 for unknown op_id; 409 on lock contention.
- `GET /api/events?op_id=X&last_event_id=Y` — SSE feed via
  `sse_starlette.EventSourceResponse`. Cursor is the SQLite rowid
  (integer, monotonic on inserts). 100ms poll interval, 5s idle
  keep-alive. Payload shape matches the design doc:
  `{event_id, cursor, op_id, op_class, phase, repo_id, student_id, status, error_class?, error_message?, commit_sha?, pr_number?}`.

**Single-flight semantics**: the lock is `state.in_flight is not None`
(no asyncio.Lock — the synchronous check-and-set inside one event-loop
iteration is atomic enough under single-process uvicorn). If we ever
run multi-worker, we'd need a DB-level lock instead.

**Two production fixes** that the SSE tests forced into Step 2:

1. **Host-header allowlist is now a pure-ASGI middleware** (not
   `@app.middleware("http")`). The decorator routes through
   Starlette's BaseHTTPMiddleware which buffers the entire response
   body — that silently breaks SSE chunk delivery. The pure-ASGI form
   sidesteps the buffer entirely. Existing host-allowlist tests still
   pass unchanged.
2. **`api_plan` passes the DB path into the executor** (not an
   already-open Database handle). `sqlite3.Connection` objects can
   only be used from the thread that opened them, and FastAPI's
   `run_in_executor` dispatches to a worker thread.

**Startup orphan-sweep**: a lifespan hook rewrites `events` rows with
`status='in-progress'` older than 60s to `status='failed'`,
`error_class='server_restart'`, `error_message='process exited mid-apply'`.
The CLI never produces `in-progress` (that's a GUI-layer status for the
background-task lifecycle in future steps); the sweep lands now so a
future rollout is safe from crash-orphaned ops.

**One Step-2 caveat for the frontend (Step 6 will care)**: the SSE
feed only emits ONE event per repo with the *final* status (the runner
INSERTs once then UPDATEs in place; the rowid cursor doesn't pick up
updates). The design doc's `planned → in-progress → applied | failed`
tile transitions can't be driven entirely from SSE. The frontend
should: render tiles in `planned` state when Apply is clicked, then
update each tile to the final status as SSE events arrive. If genuine
in-progress visibility becomes important, the runner could INSERT a
new row per status change instead of UPDATEing — small refactor.

**Open Question still unanswered:** v0.3.0 version bump strategy.
Default plan: bump to `0.3.0.dev0` at the start of Step 2 to signal
in-development, then `0.3.0` at Step 11. Confirm with the user before
the first bump.

**Don't skip the assignment from the design doc:** *"Before writing
tacon/server.py: draft the 5 verb-cards in plain Markdown and show them
to one non-technical TA."* The assignment specifically applies to the
verb-cards which land in Step 5. It's worth doing before Step 5 even
though Step 4 (settings page) is the literal next coding step.

---

## 8 · TL;DR for the impatient

1. Run `pytest -q --no-cov --ignore=tests/live`, `ruff check .`,
   `mypy tacon` to confirm nothing broke since the handoff. Expect
   **411 unit tests passing** (+18 live tests + 1 skip-on-403 if
   `TACON_LIVE=1`), ruff clean, mypy clean across **24 source files**.
2. **v0.3 GUI is in progress.** Read the design doc at
   `~/.gstack/projects/tacon/tzun-main-design-20260513-160839.md`
   end-to-end. Steps 0-3 shipped. **Step 4 is the natural next**: the
   settings page (~5h). The SPA shell is on disk at `tacon/web/`;
   `create_app()` already serves the built bundle at `/`.
3. **Steps 4-11 are ~50 hours over 3 weekends.** Don't try to do it all
   in one session. Logical milestones: after Step 5.5 (renderer), after
   Step 6 (AddFile spine — the keystone), after Step 8 (rollback +
   Past Ops), v0.3.0 release.
4. **Push unpushed commits** — `ec6d384` + `b2c2318` (Steps 0/1), the 4
   Step 2 commits (`0511eb2`, `c188a46`, `7b19251`, `6cb8895`), the 3
   Step 3 commits (`8a52c31`, `4f16ac2`, `fa8012e`) plus this handoff
   sit above `origin/main`. Direct push to `main` is blocked by Claude
   Code's permission default — the user runs `git push origin main`
   themselves.
5. **For v0.3 work specifically:** the design doc is the contract. Every
   step has an acceptance criterion. Honor them — the spec review
   surfaced 17 gaps that were patched, and skipping the criteria
   re-opens them.
6. **Before Step 4 — two items need the user's input** (the Step 3
   session deliberately paused here):
   - **Wheel-bundling of `tacon/web/dist/`.** Design doc Step 10 bundles
     the built SPA into the Python wheel via hatch `force-include` /
     `shared-data`. The exact packaging config (and whether `dist/`
     should be gitignored-but-CI-built vs committed) is a
     confirm-with-user decision — don't guess it. `dist/` is currently
     gitignored; CI builds it fresh (`make gui-dev` does the same
     locally).
   - **Bump to `0.3.0.dev0`.** Still the open versioning question —
     design doc flagged it; not bumped yet to keep PyPI metadata clean.
7. **Pre-existing TL;DR points still apply:**
   - **`twine upload dist/tacon-0.2.0*`** — §4.6/§4.8/§4.9 + v0.3
     Steps 0-3 shipped *after* the 0.2.0 build, so the user should
     bump version + rebuild before uploading.
   - The SPA needs `node` (≥20) + `pnpm` (≥9) to build. Both were
     present in the Step 3 environment (`node v22`, `pnpm 11`).
8. Use `/plan-eng-review` for substantial new modules; `/qa` for live
   GUI testing once a build is shippable; `/review` before each v0.3
   milestone commit.
9. `/context-save` at the end of your session and update this file.

---

## 10 · Resume prompt for the next session

When the next session starts, paste this block as the first user message
so the agent picks up exactly where this one left off:

> Read `/home/tzun/repos/gstack-test/tacon/next_session.md` top-to-bottom
> (focus §-1 commit story, §1 code map, §9 v0.3 GUI roadmap) and the
> design doc at
> `~/.gstack/projects/tacon/tzun-main-design-20260513-160839.md` (focus
> the "Op Schema → Form Bridge" section, "Sub-scope for v0.3" item 7
> (settings page), "Distribution Plan", and **Step 4 in Next Steps** —
> that's your target).
>
> State: tacon v0.3 GUI is in progress on `main`. Steps 0-3 of 11 are
> done. Working tree clean. **411 unit tests pass**; ruff + mypy clean
> across 24 source files. The Step 3 commits (`8a52c31`, `4f16ac2`,
> `fa8012e`) + the Step 3 handoff-doc commit are **unpushed** — I'll
> grant the push when ready.
>
> The SPA scaffold lives at `tacon/web/` (Vite + React + shadcn +
> TanStack Query). Build it with `make gui-dev` before any live GUI
> testing. `tacon serve` already serves the built bundle at `/`.
>
> **Before implementing Step 4, surface these to me — don't guess:**
> 1. **Wheel-bundling of `tacon/web/dist/`** (design doc Step 10). I
>    want to decide the hatch packaging config and the
>    gitignored-vs-committed question before it's wired.
> 2. **The `0.3.0.dev0` version bump** — still open; confirm with me.
>
> Then implement **Step 4 — settings page** (~5h) per the design doc:
> GitHub token (POST → keyring, fallback to `~/.tacon/.token` with a
> warning banner), classroom add/list/set-default via the existing
> `tacon.classes` API, rate limit, default port. Form auto-generated
> from a Pydantic settings model. Acceptance: set token in browser →
> restart server → token retrieved from keyring (Python-level
> integration test, not Playwright).
>
> Commit atomically with the existing prefix convention. Run
> pytest + ruff + mypy before each commit. Permission to push to
> `origin main` is not standing — I'll grant it when you're ready.
