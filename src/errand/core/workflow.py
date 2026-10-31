"""Moves one run forward. All decisions live here; models only supply structured facts about the user's words."""

import time

from errand.contract import AREAS, SERVICES, ToolError
from errand.core import render
from errand.core.approval import approve, move, propose, reopen, void, void_pending
from errand.core.domain import InvalidModelOutput, Run, State
from errand.core.intake import Kind, classify
from errand.core.memory import InvalidMemoryValue, check_proposal
from errand.core.turn import Turn
from errand.core.when import normalise_time, resolve_when
from errand.interpret.model import Model, ModelInput, parse_output

# Display choices for how much to show at once, not requirements.
MAX_OPTIONS = 5
MAX_SLOTS_PER_PROVIDER = 2
# After this many unusable model outputs in one turn, ask the user instead of trying again.
MAX_INVALID_MODEL_OUTPUTS = 2
NO_LIMIT = "no_limit"
SAME_AS_LAST = "same_as_last"
REQUIRED_DETAILS = {"book": ("service", "area", "when", "budget"), "reschedule": ("area", "when"), "cancel": ()}


class Workflow:
    def __init__(self, store, gateway, model: Model, memory, executor, clock):
        self.store = store
        self.gateway = gateway
        self.model = model
        self.memory = memory
        self.executor = executor
        self.clock = clock

    # One user message.

    def handle(self, turn: Turn, last_reply_id: int | None) -> None:
        intake = classify(turn.text)
        run = turn.run
        if intake.kind in (Kind.MEMORY_VIEW, Kind.MEMORY_FORGET, Kind.MEMORY_SET):
            self._memory_command(turn, intake)
        if run is not None and self.store.open_attempt(run.id) is not None:
            self._handle_in_flight(turn, intake)
            return
        if run is not None and intake.kind != Kind.STOP and self._drop_passed_slots(turn, run):
            self.advance(turn)
            return
        if intake.kind in (Kind.MEMORY_VIEW, Kind.MEMORY_FORGET, Kind.MEMORY_SET):
            if run is not None:
                self.advance(turn)
            return
        if intake.kind in (Kind.YES, Kind.NO):
            self._answer(turn, intake, last_reply_id)
        elif intake.kind == Kind.STOP:
            self._stop(turn)
        elif intake.kind == Kind.NUMBER and run is not None and (run.options or run.booking_choices):
            self._select(turn, run, intake.number)
            self.advance(turn)
        else:
            self._interpret(turn)
            self.advance(turn)

    def start_up(self, turn: Turn) -> None:
        """Resumes work left unfinished by a crash, before the user says anything."""
        run = turn.run
        if run.state in (State.EXECUTING, State.UNKNOWN_NEEDS_CHECK):
            move(self.store, turn.tracer, run, State.VERIFYING, "restart, status first")
        if self.store.open_attempt(run.id) is None:
            self._drop_passed_slots(turn, run)
        self.advance(turn)

    def advance(self, turn: Turn) -> None:
        run = turn.run
        if run is None:
            return
        while True:
            state = run.state
            if state == State.COLLECTING:
                if not self._step_collecting(turn, run):
                    break
            elif state == State.SEARCHING:
                self._step_searching(turn, run)
            elif state == State.PRESENTING:
                turn.say(render.options(run))
                break
            elif state == State.HOLDING:
                self._step_holding(turn, run)
            elif state == State.AWAITING_APPROVAL:
                self.show_pending_approval(turn, run)
                break
            elif state == State.EXECUTING:
                self.executor.execute(turn, run)
            elif state == State.VERIFYING:
                if not self.executor.verify(turn, run):
                    break
            elif state == State.UNKNOWN_NEEDS_CHECK:
                break
            else:
                break
        self.store.save_run(run)

    def verify_after_cap(self, turn: Turn, scope: str) -> None:
        """Cap reached with a write in flight: find out what happened, and start no new write."""
        run = turn.run
        turn.cap_scope = scope
        if run.state == State.HOLDING:
            if scope == "run":
                attempt = self.store.open_attempt(run.id)
                with self.store.tx():
                    void(self.store, turn.tracer, run, attempt.approval_id, "per-run cap during re-hold")
                    move(self.store, turn.tracer, run, State.FAILED, "per-run cap, nothing booked")
                turn.say("Nothing was booked.")
            else:
                turn.say("Nothing is booked yet. I will try again when you next reply.")
            return
        if run.state == State.EXECUTING:
            # The cap stopped this call before it was sent, so a retry counted for it was never used.
            attempt = self.store.open_attempt(run.id)
            if attempt.retries > 0:
                attempt.retries -= 1
                self.store.save_attempt(attempt)
            move(self.store, turn.tracer, run, State.VERIFYING, "cap reached, verifying only")
        if run.state == State.UNKNOWN_NEEDS_CHECK:
            move(self.store, turn.tracer, run, State.VERIFYING, "cap reached, verifying only")
        if run.state == State.VERIFYING:
            self.executor.verify(turn, run)
        self.store.save_run(run)

    def fail_for_cap(self, turn: Turn) -> None:
        run = turn.run
        with self.store.tx():
            void_pending(self.store, turn.tracer, run, "per-run cap")
            run.pending_question = None
            move(self.store, turn.tracer, run, State.FAILED, "per-run cap")

    # Bare answers and commands, settled in code.

    def _answer(self, turn: Turn, intake, last_reply_id: int | None) -> None:
        run = turn.run
        yes = intake.kind == Kind.YES
        question = run.pending_question if run is not None else None
        if question is None:
            turn.say(render.status(run if run is not None else self.store.latest_run(turn.user_id)))
            return
        if question["kind"] == "approval":
            self._answer_approval(turn, run, question["approval_id"], intake, last_reply_id)
        elif question["kind"] == "memory_confirm":
            run.pending_question = None
            if yes:
                self.memory.confirm(turn, question["key"])
            else:
                self.memory.reject(turn, question["key"])
            self.advance(turn)
        elif question["kind"] == "duplicate_check":
            run.pending_question = None
            if yes:
                run.duplicate_cleared = True
                self.advance(turn)
            else:
                move(self.store, turn.tracer, run, State.ABANDONED, "user declined a second booking")
                turn.say("Okay, I have not booked anything new.")

    def _answer_approval(self, turn: Turn, run: Run, approval_id: str, intake, last_reply_id: int | None) -> None:
        approval = self.store.approval(approval_id)
        if intake.kind != Kind.YES:
            with self.store.tx():
                void(self.store, turn.tracer, run, approval_id, "user said no")
                target = State.ABANDONED if run.workflow == "cancel" else State.PRESENTING
                move(self.store, turn.tracer, run, target, "user said no")
            if run.workflow == "cancel":
                turn.say("Okay, your booking stays as it is.")
                return
            self.advance(turn)
            return
        if intake.names_action is not None and intake.names_action != approval.action:
            turn.say(f"That sounds like a {intake.names_action}, and this is not one, so I have not acted on it.")
            self.advance(turn)
            return
        if approval.shown_reply_id is None or approval.shown_reply_id != last_reply_id:
            turn.tracer.event("stale_confirmation", approval_id=approval_id, shown_reply_id=approval.shown_reply_id, last_reply_id=last_reply_id)
            turn.say("That confirmation was sent before you saw these details, so I have not acted on it.")
            self.advance(turn)
            return
        if not approve(self.store, turn.tracer, run, approval_id, turn.message_id):
            turn.say(render.status(run))
            return
        self.executor.checkpoint("after_approve")
        self.advance(turn)

    def _stop(self, turn: Turn) -> None:
        run = turn.run
        if run is None:
            turn.say("There is nothing in progress to stop.")
            return
        with self.store.tx():
            void_pending(self.store, turn.tracer, run, "user stopped")
            run.pending_question = None
            move(self.store, turn.tracer, run, State.ABANDONED, "user stopped")
        turn.say("Stopped. I have not booked or changed anything for this errand.")

    def _memory_command(self, turn: Turn, intake) -> None:
        run = turn.run
        if intake.kind == Kind.MEMORY_VIEW:
            turn.say(render.memory_view(self.memory.view(turn.user_id)))
        elif intake.kind == Kind.MEMORY_FORGET:
            self.memory.forget(turn, run, intake.key)
            turn.say(f"Forgotten: your {intake.key.replace('_', ' ')}. I will not use it again unless you tell me.")
        else:
            try:
                value = self.memory.state(turn, run, intake.key, intake.value, evidence="user message")
            except InvalidMemoryValue as error:
                turn.say(f"I could not save that: {error}.")
                return
            turn.say(f"Saved: your {intake.key.replace('_', ' ')} is {value}.")

    def _drop_passed_slots(self, turn: Turn, run: Run) -> bool:
        """Options and proposals go stale while the user is away, and nothing later checks that a slot is still ahead."""
        if run.state not in (State.PRESENTING, State.HOLDING, State.AWAITING_APPROVAL):
            return False
        slots = [option["slot"] for option in run.options]
        if run.selected:
            slots.append(run.selected["slot"])
        question = run.pending_question
        if question and question["kind"] == "approval":
            slots.append(self.store.approval(question["approval_id"]).slot)
        if not any(self._has_passed(slot) for slot in slots):
            return False
        turn.say("Some of those times have passed, so I have not acted on that. Let me check what is open now.")
        if run.workflow == "cancel":
            run.target_booking = None
        reopen(self.store, turn.tracer, run, "a slot has passed")
        return True

    def _has_passed(self, slot: str) -> bool:
        return slot <= self.clock.now().strftime("%Y-%m-%dT%H:%M")

    def _handle_in_flight(self, turn: Turn, intake) -> None:
        run = turn.run
        if intake.kind == Kind.STOP:
            run.stop_requested = True
            self.store.save_run(run)
        if run.state in (State.EXECUTING, State.UNKNOWN_NEEDS_CHECK):
            move(self.store, turn.tracer, run, State.VERIFYING, "checking the outcome before anything else")
        self.advance(turn)
        if intake.kind in (Kind.OTHER, Kind.NO, Kind.NUMBER):
            turn.say("Your last message was not applied, because I had to settle this booking first. Please send it again, or ask me to reschedule if it was booked.")

    # Model interpretation.

    def _interpret(self, turn: Turn) -> None:
        invalid = 0
        read_results: list[dict] = []
        while True:
            turn.budget.charge_model()
            started = time.perf_counter()
            model_input = self._model_input(turn, read_results)
            turn.tracer.event("model_call_started")
            reply = self.model.decide(model_input, turn.budget.remaining_seconds())
            turn.tracer.event(
                "model_call",
                model=reply.served_by,
                input=model_input.to_dict(),
                raw_output=reply.raw,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                prompt_tokens=reply.prompt_tokens,
                completion_tokens=reply.completion_tokens,
                compute_seconds=reply.compute_seconds,
            )
            try:
                output = parse_output(reply.raw)
                self._check_actions(turn, output.actions)
                if self._refuses_another_errand(turn, output.actions):
                    return
                # An output is applied whole, and a details action needs the run its intent creates.
                for action in sorted(output.actions, key=lambda a: a["type"] != "set_intent"):
                    self._apply_action(turn, action, read_results)
            except (InvalidModelOutput, InvalidMemoryValue) as error:
                invalid += 1
                turn.tracer.event("model_output_rejected", detail=str(error))
                if invalid >= MAX_INVALID_MODEL_OUTPUTS:
                    turn.say("Sorry, I did not understand that. Could you say it another way?")
                    return
                continue
            # A stop ends the turn even if a model asks to continue, because nothing is left to continue.
            if output.done or any(action["type"] == "stop" for action in output.actions):
                if turn.run is None and not turn.lines:
                    turn.say(render.WHAT_I_DO)
                return

    def _model_input(self, turn: Turn, read_results: list[dict]) -> ModelInput:
        run = turn.run
        model_input = ModelInput(message=turn.text, today=self.clock.now().date().isoformat(), read_results=list(read_results))
        if run is not None:
            model_input.state = run.state.value
            model_input.workflow = run.workflow
            model_input.details = {key: entry["value"] for key, entry in run.details.items()}
            model_input.missing = self._missing(run)
            model_input.options = [{"n": o["n"], "provider_id": o["provider_id"], "price": o["price"], "rating": o["rating"]} for o in run.options]
            model_input.booking_choices = [{"n": c["n"], "service": c["service"], "slot": c["slot"]} for c in run.booking_choices]
            model_input.pending_question = run.pending_question["kind"] if run.pending_question else None
        return model_input

    def _check_actions(self, turn: Turn, actions: list[dict]) -> None:
        """Rejects a whole output before any of it applies, so a rejected output changes nothing."""
        run = turn.run
        intents = {a["intent"] for a in actions if a["type"] == "set_intent"}
        if len(intents) > 1:
            raise InvalidModelOutput(f"more than one intent: {sorted(intents)}")
        if sum(a["type"] == "select_option" for a in actions) > 1:
            raise InvalidModelOutput("more than one selection")
        if len(actions) > 1 and any(a["type"] == "stop" for a in actions):
            raise InvalidModelOutput("a stop mixed with other actions")
        for action in actions:
            kind = action["type"]
            if kind == "update_details":
                if run is None and not intents:
                    raise InvalidModelOutput("details given before any intent")
                for key, value in action["details"].items():
                    self._clean_detail(key, value)
            elif kind == "select_option":
                self._check_selection(run, action["option"])
            elif kind == "request_read" and action["tool"] == "get_provider":
                self._option(run, action.get("option"))
            elif kind == "propose_memory":
                check_proposal(action["key"], action["value"], turn.text)

    def _check_selection(self, run: Run | None, number: int) -> None:
        if run is not None and run.booking_choices:
            if not any(c["n"] == number for c in run.booking_choices):
                raise InvalidModelOutput(f"option was not presented: {number!r}")
            return
        self._option(run, number)
        if run.state not in (State.PRESENTING, State.AWAITING_APPROVAL):
            raise InvalidModelOutput(f"cannot select an option in state {run.state}")

    @staticmethod
    def _option(run: Run | None, number) -> dict:
        option = next((o for o in run.options if o["n"] == number), None) if run is not None else None
        if option is None:
            raise InvalidModelOutput(f"option was not presented: {number!r}")
        return option

    @staticmethod
    def _refuses_another_errand(turn: Turn, actions: list[dict]) -> bool:
        run = turn.run
        if run is None or not any(a["type"] == "set_intent" and a["intent"] != run.workflow for a in actions):
            return False
        turn.say(f"We are still working on your {run.workflow} request. Finish it first, or say stop to abandon it.")
        return True

    def _apply_action(self, turn: Turn, action: dict, read_results: list[dict]) -> None:
        kind = action["type"]
        if kind == "set_intent":
            self._set_intent(turn, action["intent"])
        elif kind == "update_details":
            self._update_details(turn, action["details"])
        elif kind == "select_option":
            self._select(turn, turn.run, action["option"])
        elif kind == "request_read":
            self._read_for_model(turn, action, read_results)
        elif kind == "propose_memory":
            self.memory.propose_from_model(turn, turn.run, action["key"], action["value"])
        elif kind == "need_clarification":
            turn.say("Could you tell me a little more?")
        elif kind == "stop":
            self._stop(turn)

    def _set_intent(self, turn: Turn, intent: str) -> None:
        if turn.run is not None:
            return
        run = self.store.create_run(turn.user_id, intent)
        # This turn's calls happened before the run existed, and they still count against it.
        run.model_calls = turn.budget.model_calls
        run.tool_calls = turn.budget.tool_calls
        turn.run = run
        turn.budget.attach(run)
        turn.tracer.bind(run.id)
        turn.tracer.event("run_started", run_id=run.id, workflow=intent)

    def _update_details(self, turn: Turn, details: dict) -> None:
        run = turn.run
        if run is None:
            raise InvalidModelOutput("details given before any intent")
        cleaned = {key: self._clean_detail(key, value) for key, value in details.items()}
        changed = {key: value for key, value in cleaned.items() if run.detail(key) != value}
        for key, value in changed.items():
            run.details[key] = {"value": value, "source": "user_this_run"}
            if key == "provider" and value == SAME_AS_LAST:
                run.details.pop("provider_id", None)
        if not changed:
            return
        run.awaiting_change = False
        if "service" in changed:
            self._forget_what_depended_on_the_service(run)
        if run.workflow == "cancel" and "when" in changed:
            run.target_booking = None
            run.booking_choices = []
        if run.state in (State.PRESENTING, State.AWAITING_APPROVAL, State.HOLDING):
            reopen(self.store, turn.tracer, run, "correction")
        self.store.save_run(run)

    @staticmethod
    def _forget_what_depended_on_the_service(run: Run) -> None:
        run.duplicate_cleared = False
        if run.pending_question and run.pending_question["kind"] == "duplicate_check":
            run.pending_question = None
        if run.detail("provider") == SAME_AS_LAST:
            run.details.pop("provider_id", None)
        if run.workflow in ("reschedule", "cancel"):
            run.target_booking = None
            run.booking_choices = []

    def _clean_detail(self, key: str, value):
        try:
            if key == "service" and value in SERVICES:
                return value
            if key == "area" and isinstance(value, str) and value.lower() in AREAS:
                return value.lower()
            if key == "when" and isinstance(value, str):
                resolve_when(value, self.clock.now().date())
                return value.lower()
            if key == "budget" and (value == NO_LIMIT or (isinstance(value, int) and not isinstance(value, bool) and value > 0)):
                return value
            if key in ("earliest_time", "latest_time") and isinstance(value, str):
                return normalise_time(value)
            if key == "provider" and value == SAME_AS_LAST:
                return value
        except ValueError as error:
            raise InvalidModelOutput(f"bad {key}: {value!r}") from error
        raise InvalidModelOutput(f"bad {key}: {value!r}")

    def _read_for_model(self, turn: Turn, action: dict, read_results: list[dict]) -> None:
        if action["tool"] == "list_bookings":
            bookings = self.gateway.list_bookings(turn, turn.user_id)
            read_results.append({"tool": "list_bookings", "bookings": [{"id": b["id"], "service": b["service"], "slot": b["slot"], "status": b["status"]} for b in bookings]})
            return
        number = action["option"]
        if any(result["tool"] == "get_provider" and result["option"] == number for result in read_results):
            return
        option = self._option(turn.run, number)
        provider = self.gateway.get_provider(turn, option["provider_id"])
        turn.say_untrusted(render.provider_details(provider))
        # Review text goes to the user as quoted data. The model is told only that it was shown.
        read_results.append({"tool": "get_provider", "option": number, "shown_to_user": True})

    # Choosing among presented options.

    def _select(self, turn: Turn, run: Run, number: int) -> None:
        if run.booking_choices:
            choice = next((c for c in run.booking_choices if c["n"] == number), None)
            if choice is None:
                turn.say(f"{number} is not one of the choices listed.")
                return
            run.target_booking = {k: choice[k] for k in ("id", "provider", "service", "slot", "status")}
            run.booking_choices = []
            return
        option = next((o for o in run.options if o["n"] == number), None)
        if option is None:
            turn.say(f"{number} is not one of the choices listed.")
            return
        if run.state == State.AWAITING_APPROVAL:
            with self.store.tx():
                void_pending(self.store, turn.tracer, run, "user chose a different option")
                move(self.store, turn.tracer, run, State.PRESENTING, "user chose a different option")
        if run.state != State.PRESENTING:
            turn.say("I am still working on the option you chose.")
            return
        run.selected = option
        if run.workflow == "book":
            move(self.store, turn.tracer, run, State.HOLDING, "option chosen")
            return
        price, label = self._price_of_booking(run.target_booking, option["price"])
        propose(self.store, turn.tracer, run, "reschedule", option["provider_id"], option["name"], option["slot"], price, label, booking_id=run.target_booking["id"])
        move(self.store, turn.tracer, run, State.AWAITING_APPROVAL, "slot chosen")

    # State steps.

    def _step_collecting(self, turn: Turn, run: Run) -> bool:
        """Returns True if the run moved on, and False if it is waiting for the user."""
        if run.pending_question is not None:
            self._repeat_question(turn, run)
            return False
        # A reason holds for the day it was found. After that, or for a run saved without one, it searches again.
        if run.awaiting_change and run.no_match_date == self.clock.now().date().isoformat():
            turn.say(run.no_match_reason)
            return False
        if run.workflow in ("reschedule", "cancel") and run.target_booking is None:
            if not self._find_target(turn, run):
                return run.state != State.COLLECTING
        # Before memory, because asking for the same one as last time overrides a saved technician.
        if run.detail("provider") == SAME_AS_LAST and run.detail("provider_id") is None and run.detail("service") is not None:
            self._resolve_same_as_last(turn, run)
        if self.memory.apply_to_run(turn, run):
            return False
        missing = self._missing(run)
        if missing:
            turn.say(render.ask_missing(missing))
            return False
        if run.workflow == "cancel":
            return self._propose_cancel(turn, run)
        if run.workflow == "book" and not run.duplicate_cleared and self._asks_about_duplicate(turn, run):
            return False
        move(self.store, turn.tracer, run, State.SEARCHING, "details known")
        return True

    def _step_searching(self, turn: Turn, run: Run) -> None:
        missing = self._missing(run)
        if missing:
            move(self.store, turn.tracer, run, State.COLLECTING, "a detail is now missing")
            return
        date_range = resolve_when(run.detail("when"), self.clock.now().date())
        service = run.detail("service") or run.target_booking["service"]
        providers = self.gateway.search_providers(turn, service, run.detail("area"), date_range)
        active = self._active_bookings(turn) if run.workflow == "book" else []
        run.options = self._build_options(run, providers, active)
        technician = run.details.get("provider_id")
        if not run.options and technician is not None and technician["source"] == "memory_stated":
            turn.say(f"I could not find your preferred technician {technician['value']} for this, so here is everyone who fits.")
            del run.details["provider_id"]
            run.options = self._build_options(run, providers, active)
        if not run.options:
            run.awaiting_change = True
            run.no_match_reason = self._why_nothing_matched(run, service, providers, active, date_range)
            run.no_match_date = self.clock.now().date().isoformat()
            move(self.store, turn.tracer, run, State.COLLECTING, "nothing matches")
            return
        move(self.store, turn.tracer, run, State.PRESENTING, "options found")

    def _step_holding(self, turn: Turn, run: Run) -> None:
        option = run.selected
        attempt = self.store.open_attempt(run.id)
        if attempt is not None and self._abort_rehold(turn, run, attempt):
            return
        # The contract has no way to release a hold, so a run reuses its own when the user returns to an option.
        hold = self.store.active_hold(run.id, option["provider_id"], option["slot"], self.clock.now().isoformat())
        if hold is None:
            try:
                hold = self.gateway.hold_slot(turn, option["provider_id"], option["slot"])
            except ToolError as error:
                if error.code != "slot_unavailable":
                    raise
                turn.say(f"{render.slot_text(option['slot'])} is no longer available. Here is what else is open.")
                with self.store.tx():
                    if attempt is not None:
                        void(self.store, turn.tracer, run, attempt.approval_id, "slot unavailable on re-hold")
                    run.excluded.append(f"{option['provider_id']}|{option['slot']}")
                    run.selected = None
                    run.options = []
                    move(self.store, turn.tracer, run, State.SEARCHING, "slot unavailable")
                return
            self.store.save_hold(hold["hold_id"], run.id, option["provider_id"], option["slot"], hold["quoted_price"], hold["expires_at"])
        if attempt is not None:
            approval = self.store.approval(attempt.approval_id)
            if hold["quoted_price"] == approval.price:
                attempt.hold_id = hold["hold_id"]
                self.store.save_attempt(attempt)
                move(self.store, turn.tracer, run, State.EXECUTING, "re-held on the same terms")
                return
            void(self.store, turn.tracer, run, attempt.approval_id, "price changed on re-hold")
        quoted = hold["quoted_price"]
        if quoted != option["price"]:
            turn.say(f"The provider's quote is {render.money(quoted)}, not the {render.money(option['price'])} shown earlier.")
        budget = run.detail("budget")
        if isinstance(budget, int) and quoted > budget:
            turn.say(f"That is above your budget of {render.money(budget)}.")
        propose(self.store, turn.tracer, run, "book", option["provider_id"], option["name"], option["slot"], quoted, hold_id=hold["hold_id"])
        move(self.store, turn.tracer, run, State.AWAITING_APPROVAL, "quote received")

    def _abort_rehold(self, turn: Turn, run: Run, attempt) -> bool:
        """A re-hold means the last book call changed nothing, so a stop or forget can end it here without a write."""
        if run.stop_requested:
            with self.store.tx():
                void(self.store, turn.tracer, run, attempt.approval_id, "user stopped during re-hold")
                move(self.store, turn.tracer, run, State.ABANDONED, "user stopped during re-hold")
            turn.say("Stopped. Nothing was booked.")
            return True
        if run.forgotten_in_flight:
            run.forgotten_in_flight = False
            turn.say("Nothing was booked, and I will not retry using a preference you asked me to forget.")
            with self.store.tx():
                void(self.store, turn.tracer, run, attempt.approval_id, "approved terms used a forgotten value")
                run.selected = None
                run.options = []
                move(self.store, turn.tracer, run, State.SEARCHING, "forgotten value during re-hold")
            return True
        if self._has_passed(run.selected["slot"]):
            turn.say("Nothing was booked, because that time has now passed. Here is what else is open.")
            with self.store.tx():
                void(self.store, turn.tracer, run, attempt.approval_id, "approved slot has passed")
                run.selected = None
                run.options = []
                move(self.store, turn.tracer, run, State.SEARCHING, "approved slot has passed")
            return True
        return False

    def show_pending_approval(self, turn: Turn, run: Run) -> None:
        approval = self.store.approval(run.pending_question["approval_id"])
        turn.say(render.proposal(approval))
        turn.shown_approval_id = approval.id

    def _repeat_question(self, turn: Turn, run: Run) -> None:
        question = run.pending_question
        if question["kind"] == "memory_confirm":
            turn.say(f"Please answer yes or no: should I use and keep your saved {question['key'].replace('_', ' ')}?")
        elif question["kind"] == "duplicate_check":
            turn.say("Please answer yes or no: do you want another booking for this service?")

    # Helpers.

    def _missing(self, run: Run) -> list[str]:
        if run.workflow in ("reschedule", "cancel") and run.target_booking is None:
            return ["booking"] if run.booking_choices else []
        return [key for key in REQUIRED_DETAILS[run.workflow] if run.detail(key) is None]

    @staticmethod
    def _is_wanted(run: Run, provider: dict) -> bool:
        wanted = run.target_booking["provider"] if run.workflow == "reschedule" else run.detail("provider_id")
        return wanted is None or wanted.lower() in (provider["id"], provider["name"].lower())

    def _open_slots(self, run: Run, provider: dict, active: list[dict]) -> list[str]:
        """Slots that are still ahead and not already ruled out, before any time limit the user set."""
        now_slot = self.clock.now().strftime("%Y-%m-%dT%H:%M")
        taken = {(b["provider"], b["slot"]) for b in active}
        return [slot for slot in provider["free_slots"] if slot > now_slot and f"{provider['id']}|{slot}" not in run.excluded and (provider["id"], slot) not in taken]

    def _why_nothing_matched(self, run: Run, service: str, providers: list[dict], active: list[dict], date_range: tuple[str, str]) -> str:
        """Names the filter that really decided it. Budget is only blamed if someone within the other limits would be free."""
        area = run.detail("area")
        wanted = [provider for provider in providers if self._is_wanted(run, provider)]
        if not wanted:
            return render.provider_not_found(service, area)
        free = [provider for provider in wanted if self._within_time_limits(run, self._open_slots(run, provider, active))]
        if not free:
            if any(self._open_slots(run, provider, active) for provider in wanted):
                return render.outside_time_limits(*date_range, run.detail("earliest_time"), run.detail("latest_time"))
            return render.no_free_slots(*date_range)
        # Someone is free and yet nothing was offered, so the budget is the only filter left.
        budget = run.detail("budget")
        cheapest_free = min(provider["price"] for provider in free)
        if run.workflow == "reschedule" or run.detail("provider_id") is not None:
            return render.above_this_provider(budget, cheapest_free)
        if cheapest_free == min(provider["price"] for provider in wanted):
            return render.below_budget(budget, service, area, cheapest_free)
        return render.budget_blocks_free_slots(budget, *date_range, cheapest_free)

    @staticmethod
    def _within_time_limits(run: Run, slots: list[str]) -> list[str]:
        earliest = run.detail("earliest_time")
        latest = run.detail("latest_time")
        return [slot for slot in slots if not (earliest and slot[11:16] < earliest) and not (latest and slot[11:16] >= latest)]

    def _build_options(self, run: Run, providers: list[dict], active: list[dict]) -> list[dict]:
        budget = run.detail("budget")
        candidates = []
        for provider in providers:
            if not self._is_wanted(run, provider):
                continue
            if isinstance(budget, int) and provider["price"] > budget:
                continue
            for slot in self._within_time_limits(run, self._open_slots(run, provider, active))[:MAX_SLOTS_PER_PROVIDER]:
                candidates.append((provider["price"], -provider["rating"], slot, provider))
        candidates.sort(key=lambda c: (c[0], c[1], c[2]))
        return [
            {"n": index, "provider_id": provider["id"], "name": provider["name"], "slot": slot, "price": price, "rating": provider["rating"]}
            for index, (price, _, slot, provider) in enumerate(candidates[:MAX_OPTIONS], start=1)
        ]

    def _active_bookings(self, turn: Turn) -> list[dict]:
        now_slot = self.clock.now().strftime("%Y-%m-%dT%H:%M")
        return [b for b in self.gateway.list_bookings(turn, turn.user_id) if b["status"] == "confirmed" and b["slot"] > now_slot]

    def _asks_about_duplicate(self, turn: Turn, run: Run) -> bool:
        same_service = [b for b in self._active_bookings(turn) if b["service"] == run.detail("service")]
        if not same_service:
            return False
        run.pending_question = {"kind": "duplicate_check"}
        turn.say(f"You already have a booking for {render.service_text(run.detail('service'))} on {render.slot_text(same_service[0]['slot'])}. Do you want another one? Reply yes or no.")
        return True

    def _find_target(self, turn: Turn, run: Run) -> bool:
        """Returns True once the target is known. Otherwise it has asked, or moved the run to FAILED."""
        now_slot = self.clock.now().strftime("%Y-%m-%dT%H:%M")
        bookings = [b for b in self.gateway.list_bookings(turn, turn.user_id) if b["status"] == "confirmed" and b["slot"] > now_slot]
        service = run.detail("service")
        if service is not None:
            bookings = [b for b in bookings if b["service"] == service]
        if run.workflow == "cancel" and run.detail("when") is not None:
            start, end = resolve_when(run.detail("when"), self.clock.now().date())
            bookings = [b for b in bookings if start <= b["slot"][:10] <= end]
        if not bookings:
            run.result = {"summary": "No matching upcoming booking."}
            turn.say("I could not find an upcoming booking that matches, so nothing has changed.")
            move(self.store, turn.tracer, run, State.FAILED, "no matching booking")
            return False
        if len(bookings) > 1:
            run.booking_choices = [{"n": index, **booking} for index, booking in enumerate(bookings, start=1)]
            turn.say(render.booking_choices(run))
            return False
        run.target_booking = bookings[0]
        return True

    def _resolve_same_as_last(self, turn: Turn, run: Run) -> None:
        service = run.detail("service")
        bookings = [b for b in self.gateway.list_bookings(turn, turn.user_id) if b["service"] == service]
        if not bookings:
            del run.details["provider"]
            turn.say(f"I could not find an earlier {render.service_text(service)} booking, so I will look at all providers.")
            return
        latest = max(bookings, key=lambda b: b["slot"])
        run.details["provider_id"] = {"value": latest["provider"], "source": "user_this_run"}

    def _propose_cancel(self, turn: Turn, run: Run) -> bool:
        target = run.target_booking
        price = self.store.booking_price(target["id"])
        label = ""
        name = target["provider"]
        if price is None:
            if run.detail("area") is None:
                turn.say(render.ask_missing(["area"]))
                return False
            day = target["slot"][:10]
            providers = self.gateway.search_providers(turn, target["service"], run.detail("area"), (day, day))
            match = next((p for p in providers if p["id"] == target["provider"]), None)
            if match is None:
                run.result = {"summary": "No price available, so the booking was left unchanged."}
                turn.say("I cannot find a price for this booking, so I cannot ask you to approve it. Your booking is unchanged.")
                move(self.store, turn.tracer, run, State.FAILED, "no price available")
                return True
            price, label, name = match["price"], "current price", match["name"]
        propose(self.store, turn.tracer, run, "cancel", target["provider"], name, target["slot"], price, label, booking_id=target["id"])
        move(self.store, turn.tracer, run, State.AWAITING_APPROVAL, "booking and price known")
        return True

    def _price_of_booking(self, target: dict, current_price: int) -> tuple[int, str]:
        saved = self.store.booking_price(target["id"])
        return (saved, "") if saved is not None else (current_price, "current price")

