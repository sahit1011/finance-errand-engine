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

## 6. The output schema forced the live model to invent a provider preference

- **What broke:** the first run against the real model turned "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500" into `{"service":"plumber","when":"this_week","latest_time":"12:00","provider":"same_as_last"}`. The budget was gone, and a preference the user never stated had appeared. The output was valid, so nothing rejected it. Every scripted test passed, because scripted models skip the grammar.
- **Why:** llama.cpp's grammar fixes the order of an object's keys. Once the model had written `latest_time` and a comma, `provider` was the only legal next key, and `budget` could no longer be written at all.
- **Fix:** each detail is now its own action. A list can take details in any order and can end at any point, so no key is ever forced (`output_schema` in `src/errand/interpret/llama.py`).
- **Regression test:** `test_the_schema_cannot_force_a_detail_the_user_never_gave` in `tests/unit/test_llama_model.py`.

## 7. The live model repeated itself until a cap stopped it

- **What broke:** asking about reviews printed the same provider five times and ended with "I reached my limit on thinking steps". Then "the cheapest one" replied "Sorry, I did not understand that" just above the correct proposal. The trace showed `request_read` five times, and `select_option` three times with two rejected in `HOLDING`.
- **Why:** the model could return `done: false`, which asks for another step on the same message. Nothing new reaches it on that step, so at temperature 0 it returned the same action again. Scripted models returned exactly what each test wanted, so this never appeared.
- **Fix:** the live schema fixes `done` to true, so the grammar cannot express a second step. The caps still stop any other model that loops.
- **Regression test:** `test_a_live_model_cannot_ask_for_a_second_step_on_one_message` in `tests/unit/test_llama_model.py`.

## 8. "yes?" approved a booking

- **What broke:** `test_a_question_is_not_an_approval` failed: with a proposal showing, "book it?", "ok?" and "yes?" each counted as approval. A fresh review spotted it, and the test reproduced it. This breaks must-have 2 directly, in the rule that "yes, but" was built to protect.
- **Why:** intake removed all trailing punctuation before matching the list of bare confirmations, including the question mark that shows the user is asking, not agreeing.
- **Fix:** intake no longer removes a question mark before matching confirmations (`_normalise` in `src/errand/core/intake.py`).
- **Regression tests:** `test_a_question_is_not_an_approval` in `tests/evals/test_model_paths.py`, and `test_a_question_is_not_a_confirmation` in `tests/unit/test_intake.py`.

## 9. A stop was forgotten after one turn, and the next message booked anyway

- **What broke:** `test_a_stop_given_while_the_outcome_is_unknown_still_applies_on_the_next_message` failed. `book` timed out and status gave no answer. The user said stop and was told "your stop takes effect once it is known". Their next message made a second `book` call and replied "Booked and verified". A restart did the same.
- **Why:** the stop was held on the `Turn` object, which lasts one message, so the promise made to the user could not survive to the moment it had to be kept.
- **Fix:** the stop is saved on the run (`stop_requested` in `src/errand/core/domain.py`).
- **Regression tests:** both `test_a_stop_given_while_the_outcome_is_unknown_*` tests in `tests/evals/test_model_paths.py`.

## 10. "book it" approved a cancel

- **What broke:** `test_words_naming_booking_cannot_approve_a_cancel` failed: with a cancel proposal showing, "book it" replied "Cancelled and verified". A fresh review found it while checking new Hindi words such as "book kar do", which widened an existing gap.
- **Why:** intake reduced every confirmation to a plain yes, so the action the user named was lost before it could be compared with the action on the proposal.
- **Fix:** a confirmation that names an action keeps that name, and it cannot confirm a different action (`names_action` in `src/errand/core/intake.py`, checked in `_answer_approval` in `src/errand/core/workflow.py`).
- **Regression tests:** `test_words_naming_booking_cannot_approve_a_cancel` and `test_words_naming_booking_cannot_approve_a_reschedule` in `tests/evals/test_model_paths.py`.
