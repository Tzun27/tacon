# TODOS

This sandbox repo (gstack-test) is hosting the design work for `tacon` —
a TA workbench for GitHub Classroom. The actual implementation will
live in its own repo once weekend 1 starts.

## Where the work is tracked

The single source of truth for `tacon`'s plan is the design doc:

  `~/.gstack/projects/gstack-test/tzun-main-design-20260505-155527.md`

Until the `tacon` repo is created, all deferred work is captured there
in dedicated sections:

- **`## NOT in scope`** — items considered during eng review and
  explicitly deferred (TUI cuts, additional Ops, multi-class, etc.)
- **`## Open Questions`** — design decisions deferred until the v0.1
  build surfaces enough information to answer them well
- **`## v2 Roadmap`** — Discord ingestion, auto-wiki, TA-approval
  nudges, lazy-clone infrastructure, multi-Op catalog

## When this file becomes useful

On day 1 of weekend 1, when the `tacon/` repo is initialized, this
file gets re-created inside that repo with in-flight implementation
notes (mid-build TODOs, follow-ups, "why did I do this" markers). The
design doc stays as the source of truth for v1.5 / v2 scope; TODOS.md
inside the new repo handles week-to-week in-flight notes.

## Pre-implementation actions

These came out of the eng review (D-numbers) and need to happen before
weekend 1, NOT during it:

- [ ] **Reserve `tacon` on PyPI** (D-outside-voice #5). Verified
      available 2026-05-05. Publish a 0.0.0 placeholder so the name
      doesn't get squatted before 0.1.0 ships.
- [ ] **Hour-1 spike: Textual modal+worker interaction** (D-outside-voice
      #2). Build the smallest possible Textual app that opens a
      `ModalScreen` from inside `app.run_worker(thread=True)`. If the
      Pilot test harness chokes on it, fall back to `rich.prompt`
      before committing weekend 2 to Textual.
