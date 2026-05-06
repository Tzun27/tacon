# Next session — pick up tacon v0.0.1 → v0.1

You (the next agent) are continuing work on **tacon**, a TA workbench for GitHub Classroom. The v0.0.1 foundation is fully built, all gates green, but **nothing has been committed to git yet** and several v0.1 items remain.

This file is your handoff. Read it top-to-bottom before doing anything else.

---

## 0 · Orient yourself (5 min)

```bash
cd /home/tzun/repos/gstack-test/tacon
git -C .. status --short          # see what's untracked
.venv/bin/pytest -q                # confirm 61 passing
.venv/bin/ruff check . && .venv/bin/mypy tacon   # confirm gates still green
```

Expected after that:
- Parent repo `/home/tzun/repos/gstack-test/` shows `?? tacon/`, `?? TODOS.md`, ` M .gitignore` plus all the `?? .claude/skills/...` from the gstack install.
- 61 tests pass.
- Coverage 74% (gate is 70%).
- ruff + mypy clean.

If any of that fails, **stop and investigate before continuing** — something has drifted since the last session.

---

## 1 · What exists (don't rebuild)

### Code
- `tacon/__init__.py` — `__version__ = "0.0.1"`
- `tacon/db.py` — SQLite schema (5 tables: assignments, students, repos, events, interactions + meta) and accessors. Uses `_t(db, name)` helper to cast `Table | View → Table`. **Uses `.insert(..., pk=..., replace=True)` instead of `.upsert(...)` — see memory `feedback_sqlite_utils_upsert.md` for why.**
- `tacon/ops/__init__.py` — `Op` ABC + 6 dataclasses (`RepoDiff`, `Diff`, `RepoApplyResult`, `ApplyResult`, `RepoRollbackResult`, `RollbackResult`), `ConfirmCallback` type, registry (`register`/`get_op_class`/`list_ops`).
- `tacon/ops/add_file.py` — first concrete op. Plan/apply/rollback with blob-SHA equality check.
- `tacon/github_client.py` — `RateLimitedClient` (default 3 req/sec, exponential backoff), `classify_error()` returns 8 enum strings, `get_default_token()` reads env vars + falls back to `gh auth token`.
- `tacon/classroom.py` — `discover_via_gh_classroom()` (shells out to `gh classroom`), `discover_via_csv()` (fallback), `persist_discovered()`.
- `tacon/cli.py` — Typer app with commands: `sync`, `run`, `rollback`, `resume`, `version`. **Stubs (exit 2 with "not implemented"):** `ui`, `dashboard`.

### Tests (61 passing)
- `tests/conftest.py` — fixtures: `tmp_db`, `seed_repos` (alice/bob/carol), `fake_repo` (MagicMock), `fake_gh` (passthrough MagicMock).
- `tests/test_db.py` — 13 tests: schema, upserts, archive, lowercase student_id, events.
- `tests/test_github_client.py` — `classify_error` matrix + RateLimitedClient throttle/retry.
- `tests/test_classroom.py` — CSV parse + gh shell-out (mocked) + persistence.
- `tests/ops/test_add_file.py` — 12 tests: plan/apply/rollback branches with mocked PyGithub. **The most important one is `test_rollback_skipped_dirty_when_blob_changed`** — it proves we never overwrite student work.
- `tests/test_cli.py` — 7 Typer CliRunner smoke tests.

### Tooling
- `pyproject.toml` — hatch build, deps (PyGithub, typer, rich, sqlite-utils, jinja2), dev extras (pytest, pytest-cov, responses, ruff, mypy), `tui` extras (textual). Coverage gate is **70%** for v0.0.1 with a TODO to raise it to 80% by v0.1 and 95% by v1.0.
- `.github/workflows/ci.yml` — matrix on Python 3.10/3.11/3.12 (ruff + mypy + pytest).
- `.venv/` — Python 3.13.5 virtualenv with everything installed (`pip install -e ".[dev]"`).
- `README.md`, `LICENSE` (MIT), `.gitignore`.

### Design artifacts (read-only references)
- **Approved design doc** (9/10 spec review): `/home/tzun/.gstack/projects/gstack-test/tzun-main-design-20260505-155527.md` — has every D1–D17 decision with rationale. Read this before making architectural decisions.
- Eng-review test plan: `/home/tzun/.gstack/projects/gstack-test/tzun-main-eng-review-test-plan-20260505-164835.md`.
- Memory: `/home/tzun/.claude/projects/-home-tzun-repos-gstack-test/memory/MEMORY.md` (currently one entry: the sqlite-utils 3.39 upsert quirk).

---

## 2 · The first thing to do: commit the foundation

**Nothing in `tacon/` is in git yet.** Before doing any new work, get this checkpointed.

Recommended order:
1. **Ask the user what scope they want committed.** The parent repo has unrelated untracked changes (the whole `.claude/skills/*` tree from the gstack install, plus a modified `.gitignore` and a `TODOS.md`). Don't sweep those into a tacon commit.
2. Stage only the tacon subtree:
   ```bash
   git -C /home/tzun/repos/gstack-test add tacon/
   ```
3. Optionally add `TODOS.md` if it points to the design doc.
4. Commit with a message like `feat: scaffold tacon v0.0.1 foundation` — body should mention 5-table schema, Op ABC, AddFile, RateLimitedClient, classroom sync, Typer CLI, 61 tests, 74% coverage.
5. **Do not push** unless the user asks. There's no remote configured for tacon yet.

**Recommended skill:** `/ship` — handles the commit + push flow with safety rails. Just be aware it'll want to push to a remote; for tacon there isn't one yet, so you may want to commit-only first and let the user decide on the remote (PyPI publish + GitHub repo creation are both still TODO per the design doc).

---

## 3 · v0.1 roadmap (pick what the user wants)

In priority order based on the design doc:

### 3.1 — More ops (highest leverage)
The Op ABC is built; adding new ops is now mostly a matter of writing one file + tests. Targets per the design doc:
- **`AddCIWorkflow`** — write `.github/workflows/X.yml`. Same shape as AddFile but with workflow-aware diffing.
- **`FixCIWorkflow`** — patch an existing workflow (e.g. bump action versions). Need a content-transform callback signature.
- **`DeleteFile`** — inverse of AddFile. Rollback restores from blob.
- **`AddBranchProtection`** — read-only at first (just report); write-mode requires admin token.

Each op needs:
- A new file in `tacon/ops/`
- `register("name", Class)` at module bottom
- An import in `tacon/cli.py` for side-effect registration (or a discovery loop — see "tech debt" §4 below)
- A `tests/ops/test_<name>.py` mirroring `test_add_file.py`'s structure (use `fake_gh` + `fake_repo` fixtures)

### 3.2 — Raise coverage to 80%
Current gaps:
- `tacon/cli.py` 35% — almost all uncovered lines are inside `run`, `rollback`, `resume` orchestration. Need integration-style CliRunner tests that mock `RateLimitedClient` at the boundary. The existing `test_cli.py` only does smoke tests.
- `tacon/github_client.py` 65% — uncovered: `get_default_token()` fallback to `gh auth token`, retry-after header parsing, the inner backoff branches.
- `tacon/ops/add_file.py` 92% — small remaining gap around lines 92–95, 261–262, 271–288 (look at the coverage report for specifics).

Once at 80%, bump `--cov-fail-under=70` → `80` in `pyproject.toml`.

### 3.3 — TUI (the "ui" stub)
Per the design doc, this is a Textual app showing per-repo plan/apply status. The `textual` dep is already declared under the `[tui]` extra in `pyproject.toml`. Start point: replace the `ui` command stub in `cli.py` with a Textual `App` subclass. Use `Pilot` for tests (test stack already includes it).

### 3.4 — Dashboard renderer (the "dashboard" stub)
Per design D16, the dashboard renders to static HTML and supports `--publish` to gh-pages. Use Jinja2 (already a dep) over the SQLite events table. Templates in `tacon/dashboard/templates/`.

### 3.5 — End-to-end test against live GitHub
All current tests mock PyGithub. Per the test plan artifact, we need at least one e2e test that hits the real GitHub API against a throwaway test classroom. Use `responses` (already a dep) for VCR-style replay, or a `--live` pytest marker for opt-in real calls.

---

## 4 · Known tech debt / gotchas

1. **sqlite-utils 3.39 upsert quirk** — already documented in memory and inline in `db.py`. If you see a write that "succeeds" but the row isn't there, suspect this first.
2. **Op registration is import-side-effect** — `cli.py` does `from tacon.ops.add_file import AddFile` to trigger `register()`. As ops multiply, switch to an entry-points discovery pattern or an explicit registry list.
3. **`resume` command is half-wired** — it identifies failed events but doesn't reconstruct the op. The current behavior prints a manual workaround. Real fix needs op_args_json deserialization (the column exists in the events table; nothing writes to it in v0.0.1 yet).
4. **`gh classroom` extension may not be installed** on the user's system. The `discover_via_gh_classroom` path raises `GhClassroomError` with a clear message when `which gh` returns None. CSV fallback is the supported workflow until the user installs it.
5. **No PyPI publish yet.** The name `tacon` is reserved (verified 404 on pypi.org/project/tacon). When publishing, bump version, build with `hatch build`, and the user will run `twine upload`.
6. **No GitHub remote.** The tacon code lives in a subdirectory of the gstack-test sandbox repo. v0.1 may want its own repo at `github.com/<user>/tacon`. Discuss before creating.
7. **Python 3.13 in the venv but CI matrix is 3.10/3.11/3.12.** Both work; just be aware. If you add 3.13-only syntax it'll break CI.

---

## 5 · How to use gstack skills here

The user has gstack installed and `CLAUDE.md` has skill routing. Recommended skills for this work:

| Situation | Skill | Why |
|---|---|---|
| Resuming context (today's first message) | `/context-restore` | Loads prior plan + notes if the user used `/context-save` last time. |
| Planning v0.1 scope | `/plan-eng-review` | Pressure-test which ops to ship first; surfaces scope/test/perf risks. |
| Brainstorming op surface area | `/office-hours` | Lower-friction than plan-eng-review for "which 3 ops are worth doing first?" |
| Before committing | `/review` | Catches bugs and inconsistencies in the diff before it lands. |
| Committing + (later) PRing | `/ship` | Safe commit + push flow. Combines well with `/land-and-deploy` once there's a remote. |
| Hunting a bug | `/investigate` | Root-cause first, fix second. |
| Verifying a feature works | `/qa` | End-to-end behavioral check. |
| Saving progress at end of session | `/context-save` | Pairs with `/context-restore` next time. |

**Heuristic:** if you're about to add a substantial new module (e.g. starting the TUI), invoke `/plan-eng-review` first. If you're polishing or debugging, skip the ceremony.

---

## 6 · Environment recap

- **Working dir:** `/home/tzun/repos/gstack-test/tacon/`
- **venv:** `/home/tzun/repos/gstack-test/tacon/.venv/` — activate with `source .venv/bin/activate` or just call `.venv/bin/<tool>` directly.
- **Default DB path:** `~/.tacon/tacon.db` (overrideable via `--db` or `TACON_HOME` env var).
- **Memory dir:** `/home/tzun/.claude/projects/-home-tzun-repos-gstack-test/memory/`
- **gstack home:** `~/.gstack/` — design docs live under `~/.gstack/projects/gstack-test/`.
- **Python in venv:** 3.13.5 (CI tests against 3.10/3.11/3.12).
- **Today's date when this was written:** 2026-05-05.

---

## 7 · TL;DR for the impatient

1. Run `pytest`, `ruff`, `mypy` to confirm nothing broke since the handoff.
2. Ask the user: **commit the foundation now, or push v0.1 work first?**
3. If they want new work, ask **which v0.1 item from §3** — most likely "another op."
4. Use `/plan-eng-review` for non-trivial new modules; otherwise just code.
5. Keep coverage gate satisfied; raise it to 80% once `cli.py` has integration tests.
