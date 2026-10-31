# Real bugs hit while building

Each entry is a failure that actually happened during the build, with the regression test that now guards it.

## 1. A crash just before the reply reported the booking twice

- **What broke:** `test_crash_before_reply` (scenario 37) failed with `assert 2 == 1`. After a restart, the user saw "Booked and verified" twice.
- **Why:** the outcome is saved in the same transaction as `DONE`, so a crash can never lose it. The turn's final reply then repeats that text and is saved as a second undelivered row. A crash between saving the reply and returning it left both rows undelivered.
- **Fix:** saving the final reply now marks the outcome rows it replaces as delivered, in one transaction, so exactly one of them can ever be shown again (`src/errand/runner.py`).
- **Regression tests:** `test_crash_before_reply` and `test_crash_between_saving_outcome_and_building_the_reply_still_reports_it` in `tests/evals/test_slice2_safety.py`.

## 2. Reaching any cap crashed the handler that should tell the user

- **What broke:** `test_model_call_cap`, `test_tool_call_cap` and `test_wall_clock_cap` failed with `TypeError: Tracer.event() got multiple values for argument 'kind'`. The cap was enforced, but the turn then died with no message to the user, which is exactly what must-have 5 forbids.
- **Why:** the first parameter of `Tracer.event` was named `kind`, and the cap handler passed a trace field also called `kind`. The failure only appears on a path that normal bookings never take.
- **Fix:** the event type is now a positional-only parameter, so no trace field name can clash with it (`src/errand/infra/trace.py`).
- **Regression tests:** scenarios 23, 24 and 25 in `tests/evals/test_slice5_hardening.py`.

## 3. The model call that created a run did not count against that run

- **What broke:** `test_per_run_cap_with_proposal_pending` (scenario 35) failed: with `run_model_calls=1`, the run stayed in `AWAITING_APPROVAL` when it should have moved to `FAILED`. The per-run cap allowed one more model call than configured.
- **Why:** a run is created by the model's own `set_intent` action, so that call was charged to the turn while no run was attached yet. The run's counter started at zero.
- **Fix:** a new run starts with the calls its creating turn has already made (`_set_intent` in `src/errand/core/workflow.py`).
- **Regression test:** `test_per_run_cap_with_proposal_pending` in `tests/evals/test_slice5_hardening.py`.

## 4. A retry blocked by a cap was still counted, which made a promise to the user false

- **What broke:** `test_cap_with_write_in_flight` (scenario 31) failed with `assert 0 == 1` in the ledger. The agent told the user "I will try again when you next reply", then on the next message said it had run out of retries, with no further `book` call.
- **Why:** a retry was counted when it was scheduled, not when it was sent. The cap then stopped that call, so a retry was used up with nothing sent.
- **Fix:** when a cap stops a call in `EXECUTING`, the retry counted for it is given back (`verify_after_cap` in `src/errand/core/workflow.py`).
- **Regression test:** `test_cap_with_write_in_flight` in `tests/evals/test_slice5_hardening.py`.

## 5. "Stop" during a re-hold could still book

- **What broke:** `test_stop_during_a_rehold_never_books` failed: the ledger held a booking after the user said stop. A code review found this path by hand-tracing, and the test reproduced it before the fix.
- **Why:** after `hold_expired`, the run sits in `HOLDING` with an approved attempt. The stop logic only ran on the way through `VERIFYING`, so a stop in `HOLDING` carried on to a new hold and then `book`, and the user was told it was "already booked when you asked to stop".
- **Fix:** a re-hold now checks for a stop or a forgotten value first, and ends the run with no write (`_abort_rehold` in `src/errand/core/workflow.py`).
- **Regression tests:** `test_stop_during_a_rehold_never_books` and `test_forget_during_a_rehold_does_not_retry_with_the_forgotten_value` in `tests/evals/test_edge_cases.py`.
