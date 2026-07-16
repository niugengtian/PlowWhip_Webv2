# Sprint 0 Evidence

## Isolation

- New repository: `/Users/niugengtian/work/plow-whip-web-v2`
- Legacy repository: `/Users/niugengtian/work/plow-whip` (read-only reference)
- Relationship: independent Git repository; no worktree, submodule, symlink, shared database, shared runtime, or Python import.
- Legacy `git status --porcelain=v1` was identical before and after copying the approved documents: 40 existing entries, zero changes introduced by Web v2 execution.

## Toolchain

- Python: 3.13 virtual environment at `.venv`
- Backend: FastAPI + Pydantic + Uvicorn + stdlib SQLite
- Frontend: React + TypeScript + Vite + Vitest + ESLint
- Data: local `runtime/`, ignored by Git

## Quality baseline

- Legacy read-only baseline: `python3 -m unittest discover -s tests -q` → 229 tests passed.
- Backend: 3 tests passed; branch coverage enabled; total initial coverage 81%.
- Backend compile: `python -m compileall -q backend tests` passed.
- Frontend: 1 test passed.
- Frontend typecheck: passed.
- Frontend lint: passed.
- Frontend production build: passed.
- SQLite migration: first run applies `0001_initial.sql`; second run applies nothing.
- `/health`: reports backend version, SQLite WAL, and migration count.
- `/api/system/capabilities`: reports `desktop_required=false` and `model_invoked=false`.

## Sprint exit decision

`pass` — the independent project skeleton is runnable and testable. No model or CLI Worker was started.
