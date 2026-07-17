# Sprint 10 Migration Contract Result

## Scope

- Role: DevOps / Database Contract Worker.
- Task: `8cb9d5b0-7e72-4805-beb8-04f978d92c10`.
- Worker: `b6f9eda2-4334-4ef7-a161-b3185a1d632d`.
- Modified only `tests/test_app.py` and `tests/test_database.py`, and added this
  result document.
- No backend implementation, migration SQL, quality-profile code, or web code was changed.
- No reset, rollback, cleanup, commit, push, or deployment was performed.

## Contract change

- Both tests derive migration filenames from
  `backend/plow_whip_web/store/migrations` using the runtime ordering rule:
  `sorted(migration_dir.glob("*.sql"))`.
- The database idempotency test requires the first `migrate()` result to match
  the disk manifest exactly and contain no duplicates.
- The same test requires a second `migrate()` call to add nothing and checks
  the health count against the derived manifest length.
- The health API test keeps the existing status and WAL assertions and checks
  `migration_count` against the derived manifest length.
- No production-only manifest abstraction was added.

## Verification evidence

| Gate | Command | Exit code | Result |
| --- | --- | ---: | --- |
| Focused migration contract | `.venv/bin/python -m pytest tests/test_app.py tests/test_database.py -q` | 0 | PASS; 3 tests passed |
| Full backend suite | `.venv/bin/python -m pytest -q` | 1 | PARTIAL; only the 2 known quality-profile cases failed |
| Diff hygiene | `git diff --check` | 0 | PASS; no whitespace errors |

The full-suite failures are:

- `tests/test_release_security.py::test_quality_profiles_have_bounded_run_shapes[balanced-expected1]`
- `tests/test_release_security.py::test_quality_profiles_have_bounded_run_shapes[strict-expected2]`

Neither failure concerns migration inventory, ordering, idempotency, duplicate
application, health count, or WAL mode. This worker did not modify the
quality-profile slice. The migration contract slice passes, but the
pre-deployment backend gate is not green.

SPRINT10_MIGRATION_CONTRACT_COMPLETE
