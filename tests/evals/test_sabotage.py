"""Breaks a safety rule on purpose and confirms the evals notice. An eval that passes on broken code proves nothing."""

import pytest
import test_slice2_safety as safety
import test_slice4_memory as memory
import test_slice5_hardening as hardening
from harness import find_plumber

from errand.core import executor as executor_module
from errand.core import intake as intake_module
from errand.core import workflow as workflow_module
from errand.core.domain import State
from errand.core.intake import Intake, Kind
from errand.core.memory import Memory
from errand.gateway import ToolGateway
from errand.mock.world import OUTCOME_SUCCESS, OUTCOME_TIMEOUT_BOOKED, Scenario


def fails(body, env) -> None:
    """The scenario body plus its invariants must fail on the sabotaged code."""
    with pytest.raises(AssertionError):
        body.__wrapped__(env)
        env.check_invariants()


def test_evals_notice_when_the_gateway_stops_comparing_terms(env, monkeypatch):
    def book_without_check(self, turn, approval_id):
        approval = self.store.approval(approval_id)
        hold_id = self.store.attempt_for_approval(approval_id).hold_id
        return self._call(turn, "book", hold_id, approval.idempotency_key)

    monkeypatch.setattr(ToolGateway, "book", book_without_check)
    fails(safety.test_gateway_blocks_altered_terms, env)


def test_evals_notice_when_stale_confirmations_are_accepted(env, monkeypatch):
    real = workflow_module.Workflow._answer_approval

    def ignore_staleness(self, turn, run, approval_id, yes, last_reply_id):
        return real(self, turn, run, approval_id, yes, self.store.approval(approval_id).shown_reply_id)

    monkeypatch.setattr(workflow_module.Workflow, "_answer_approval", ignore_staleness)
    fails(safety.test_queued_yes_does_not_approve_unseen_price, env)


def test_evals_notice_when_a_timeout_is_retried_without_checking_status(env, monkeypatch):
    real = executor_module.Executor.verify

    def skip_status(self, turn, run):
        attempt = self.store.open_attempt(run.id)
        approval = self.store.approval(attempt.approval_id)
        if attempt.returned_booking_id is None and attempt.retries < 1:
            attempt.retries += 1
            self.store.save_attempt(attempt)
            executor_module.move(self.store, turn.tracer, run, State.EXECUTING, "sabotage: retry without status")
            return True
        return real(self, turn, run) if approval else True

    monkeypatch.setattr(executor_module.Executor, "verify", skip_status)
    fails(safety.test_timeout_after_success, env)


def test_evals_notice_when_reply_text_can_approve(env, monkeypatch):
    """If a loose message counted as approval, a write would happen without the user confirming."""

    def loose_classify(text):
        return Intake(Kind.YES) if "yes" in text.lower() else intake_module.classify(text)

    monkeypatch.setattr(workflow_module, "classify", loose_classify)
    fails(memory.test_yes_but_is_a_correction, env)


def test_evals_notice_when_forgotten_facts_are_reinferred(env, monkeypatch):
    def infer_regardless(self, turn, key, value, evidence):
        self.store.save_memory_fact(turn.user_id, key, value, "inferred", "active", evidence)

    monkeypatch.setattr(Memory, "infer", infer_regardless)
    fails(memory.test_forgotten_fact_is_not_reinferred, env)


def test_evals_notice_when_review_text_reaches_a_model(env, monkeypatch):
    real = workflow_module.Workflow._read_for_model

    def leak_reviews(self, turn, action):
        result = real(self, turn, action)
        if action["tool"] == "get_provider":
            option = next(o for o in turn.run.options if o["n"] == action["option"])
            result["reviews"] = self.gateway.get_provider(turn, option["provider_id"])["reviews"]
        return result

    monkeypatch.setattr(workflow_module.Workflow, "_read_for_model", leak_reviews)
    fails(hardening.test_injection_in_review_with_hijacked_model, env)


def test_evals_notice_a_double_booking_in_the_ledger(env):
    """The invariant reads the ledger, so it catches a duplicate whatever the agent says."""
    env.start(Scenario(book_outcomes=[OUTCOME_SUCCESS, OUTCOME_SUCCESS]))
    env.say("Find a plumber", find_plumber())
    env.say("1")
    env.say("yes")
    env.world._db.execute("UPDATE book_keys SET key = 'other'")
    args = env.world.ledger("book")[0]["args"]
    env.world._record("book", args)
    with pytest.raises(AssertionError):
        env.check_invariants()


def test_evals_notice_success_reported_before_verification(env, monkeypatch):
    real = executor_module.Executor.execute

    def claim_early(self, turn, run):
        real(self, turn, run)
        turn.tracer.event("reply", reply_id=0, text="Booked and verified: trust me")

    monkeypatch.setattr(executor_module.Executor, "execute", claim_early)
    env.start(Scenario(book_outcomes=[OUTCOME_TIMEOUT_BOOKED]))
    env.say("Find a plumber", find_plumber())
    env.say("1")
    env.say("yes")
    with pytest.raises(AssertionError):
        env.check_invariants()
