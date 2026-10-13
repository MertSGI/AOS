# Test and CI results - 2026-10-13

All results below are bound to source SHA `edaab3890e7584024ebc499230f9dd7edd31ccff` unless explicitly identified as reproduction evidence.

## Completed verification

- Focused deployment/supervisor/slot/transition suite:
  - command scope: `test_runtime_transition_ordering.py`, `test_runtime_deploy.py`, `test_runtime_supervisor_identity.py`, `test_runtime_supervisor_singleton.py`, `test_runtime_slots.py`
  - result: `46 passed in 51.11s`
- Full offline/non-Postgres suite:
  - selector: `-m "not postgres_integration and not live_read_only"`
  - result: `1492 passed, 8 skipped, 26 deselected in 704.53s`
- Hosted CI:
  - workflow: `AOS CI`
  - run: `38305877797`
  - event: `push`
  - head: `edaab3890e7584024ebc499230f9dd7edd31ccff`
  - terminal result: `completed / success`
  - URL: https://github.com/MertSGI/AOS/actions/runs/38305877797

## Reproduction result

The unmodified `9dddf1e...` activation path against isolated runtime `...-01` caused the old stable supervisor to exit with the exact `RuntimeTransitionIncompleteError` in `SUPERVISOR_FAILURE_DIAGNOSTIC.json`. The overall isolated transaction later returned `PASS_MAINTENANCE` only because the candidate won the singleton race after the crash. That is diagnostic reproduction evidence, not repaired-candidate proof.

## Not completed

- No candidate for `edaab389...` was materialized.
- No official stage or validate ran for `edaab389...`.
- No isolated KCP receipt was appended for `edaab389...`.
- No repaired stable-to-candidate activation, restart/re-entry, or rollback proof ran.
- No protected runtime activation or LARI execution ran.

The canary/activation gate for `edaab389...` is therefore `INCOMPLETE_FAIL_CLOSED`, not PASS.
