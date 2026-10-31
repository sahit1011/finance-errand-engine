"""The router over real HTTP against stand-in servers, including through the real chat command."""

import json
import subprocess
import sys
import time
from threading import Timer

import pytest
from test_llama_model import StandInServer

from errand.core.domain import ModelUnavailable
from errand.interpret import router
from errand.interpret.llama import LlamaModel
from errand.interpret.model import ModelInput
from errand.interpret.router import ROUTES, RoutedModel, load_routing, route_text

ENTRY = "from errand.cli import main; raise SystemExit(main())"
EASY = route_text("COLLECTING", "easy")
HARD = route_text("COLLECTING", "hard")
CHATTER = route_text(None, "chatter")
VECTORS = {EASY: [1.0, 0.0, 0.0], HARD: [0.0, 1.0, 0.0], CHATTER: [0.0, 0.0, 1.0]}
ROUTING = {"threshold": 0.9, "examples": [{"text": EASY, "route": "cheap"}, {"text": HARD, "route": "strong"}, {"text": CHATTER, "route": "none"}]}
# Near no example, so the router should treat it as hard.
UNLIKE_ANYTHING = [0.577, 0.577, 0.577]


def embedding_of(request: dict) -> dict:
    return {"data": [{"embedding": VECTORS.get(request["input"], UNLIKE_ANYTHING)}]}


def llm_body(output: dict) -> dict:
    return {
        "choices": [{"message": {"content": json.dumps(output)}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        "timings": {"prompt_ms": 1.0, "predicted_ms": 1.0},
    }


@pytest.fixture
def servers():
    cheap, strong, embedder = StandInServer(), StandInServer(), StandInServer()
    cheap.body = llm_body({"actions": [{"type": "need_clarification"}], "done": True})
    strong.body = llm_body({"actions": [{"type": "need_clarification"}], "done": True})
    embedder.body = embedding_of
    yield cheap, strong, embedder
    for server in (cheap, strong, embedder):
        server.close()


def routed(servers) -> RoutedModel:
    cheap, strong, embedder = servers
    model = RoutedModel(LlamaModel(cheap.url), LlamaModel(strong.url), LlamaModel(embedder.url), ROUTING)
    model.check()
    return model


def llm_calls(servers) -> tuple[int, int]:
    return len(servers[0].requests), len(servers[1].requests)


def test_a_message_like_an_easy_example_goes_to_the_cheap_model(servers):
    reply = routed(servers).decide(ModelInput(message="easy", today="2026-11-02", state="COLLECTING"), 5)
    assert llm_calls(servers) == (1, 0)
    assert reply.served_by == "router:cheap"


def test_a_message_like_a_hard_example_goes_to_the_strong_model(servers):
    routed(servers).decide(ModelInput(message="hard", today="2026-11-02", state="COLLECTING"), 5)
    assert llm_calls(servers) == (0, 1)


def test_a_message_like_no_example_goes_to_the_strong_model(servers):
    routed(servers).decide(ModelInput(message="something new", today="2026-11-02"), 5)
    assert llm_calls(servers) == (0, 1)


def test_a_message_needing_no_action_reaches_no_llm(servers):
    reply = routed(servers).decide(ModelInput(message="chatter", today="2026-11-02"), 5)
    assert llm_calls(servers) == (0, 0)
    assert json.loads(reply.raw) == {"actions": [], "done": True}
    assert reply.served_by == "router:none"


def test_the_router_sees_only_the_state_and_the_message(servers):
    options = [{"n": 1, "provider_id": "p", "price": 1, "rating": 1.0}]
    routed(servers).decide(ModelInput(message="hard", today="2026-11-02", state="COLLECTING", options=options), 5)
    assert servers[2].requests[-1]["input"] == HARD


def test_a_router_server_that_is_down_is_a_named_failure(servers):
    model = routed(servers)
    servers[2].status, servers[2].body = 500, {"error": "boom"}
    with pytest.raises(ModelUnavailable, match="500"):
        model.decide(ModelInput(message="easy", today="2026-11-02"), 5)


def test_a_bad_embedding_response_is_a_named_failure(servers):
    model = routed(servers)
    servers[2].body = {"data": []}
    with pytest.raises(ModelUnavailable, match="unexpected"):
        model.decide(ModelInput(message="easy", today="2026-11-02"), 5)


def test_the_router_and_the_model_together_stay_inside_the_time_they_were_given(servers):
    """The turn cap in docs/SPECS.md must hold when routed, so the model only gets what the router left."""
    model = routed(servers)
    cheap, strong, embedder = servers
    strong.hang.set()
    embedder.hang.set()
    Timer(0.6, embedder.release.set).start()
    started = time.perf_counter()
    with pytest.raises(ModelUnavailable):
        model.decide(ModelInput(message="hard", today="2026-11-02", state="COLLECTING"), 1.0)
    assert time.perf_counter() - started < 1.4


def test_a_router_that_used_all_the_time_does_not_call_a_model(servers):
    model = routed(servers)
    servers[2].hang.set()
    Timer(0.4, servers[2].release.set).start()
    # The embedder's own timeout is shorter than its answer, so the failure names the time.
    with pytest.raises(ModelUnavailable, match="0.2"):
        model.decide(ModelInput(message="easy", today="2026-11-02", state="COLLECTING"), 0.2)
    assert llm_calls(servers) == (0, 0)


def test_an_embedding_of_all_zeros_is_a_named_failure(servers):
    model = routed(servers)
    servers[2].body = {"data": [{"embedding": [0.0, 0.0, 0.0]}]}
    with pytest.raises(ModelUnavailable, match="unexpected"):
        model.decide(ModelInput(message="easy", today="2026-11-02"), 5)


def test_an_embedding_of_a_different_size_is_a_named_failure(servers):
    model = routed(servers)
    servers[2].body = {"data": [{"embedding": [1.0, 0.0]}]}
    with pytest.raises(ModelUnavailable, match="size"):
        model.decide(ModelInput(message="easy", today="2026-11-02"), 5)


def test_a_missing_routing_file_is_a_named_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "ROUTING_FILE", tmp_path / "absent.json")
    with pytest.raises(ValueError, match="absent.json"):
        load_routing()


@pytest.mark.parametrize("content", ["not json", "{}", '{"threshold": 0.9}', '{"threshold": "high", "examples": []}', '{"threshold": 0.9, "examples": [{"text": "x", "route": "maybe"}]}'])
def test_a_bad_routing_file_is_a_named_failure(tmp_path, monkeypatch, content):
    path = tmp_path / "routing.json"
    path.write_text(content)
    monkeypatch.setattr(router, "ROUTING_FILE", path)
    with pytest.raises(ValueError, match="routing.json"):
        load_routing()


def test_the_shipped_routing_file_is_valid():
    routing = load_routing()
    assert 0 < routing["threshold"] <= 1
    assert {example["route"] for example in routing["examples"]} <= set(ROUTES)
    assert routing["examples"]


def chat(cwd, lines, *flags) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", ENTRY, "chat", *flags],
        input="".join(line + "\n" for line in lines),
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=30,
    )


def test_routed_chat_books_and_traces_which_model_served_and_approval_reaches_no_router(tmp_path, servers):
    cheap, strong, embedder = servers
    find = {"actions": [{"type": "set_intent", "intent": "book"}, {"type": "update_details", "details": {"service": "plumber"}}, {"type": "update_details", "details": {"area": "indiranagar"}}, {"type": "update_details", "details": {"when": "next_week"}}, {"type": "update_details", "details": {"budget": "no_limit"}}], "done": True}
    strong.body = llm_body(find)
    # The message resembles none of the shipped examples, so it should be treated as hard.
    message_text = route_text(None, "Find a plumber next week in Indiranagar")
    embedder.body = lambda request: {"data": [{"embedding": [1.0, 0.0, 0.0] if request["input"] == message_text else UNLIKE_ANYTHING}]}
    routing_flags = ("--model-url", cheap.url, "--strong-model-url", strong.url, "--router-url", embedder.url, "--book-outcome", "success")
    result = chat(tmp_path, ["Find a plumber next week in Indiranagar", "1", "yes"], *routing_flags)
    assert result.returncode == 0, result.stderr
    assert "Booked and verified" in result.stdout
    trace = [json.loads(line) for line in (tmp_path / "traces" / "default" / "run-1.jsonl").read_text().splitlines()]
    assert [e["model"] for e in trace if e["type"] == "model_call"] == ["router:strong"]
    assert (len(cheap.requests), len(strong.requests)) == (0, 1)
    # One embedding per shipped example, then one for the only message that needed reading. "1" and "yes" are code's.
    assert len(embedder.requests) == len(load_routing()["examples"]) + 1
    assert embedder.requests[-1]["input"] == route_text(None, "Find a plumber next week in Indiranagar")


def test_router_flags_must_come_together(tmp_path, servers):
    result = chat(tmp_path, ["hello"], "--model-url", servers[0].url, "--router-url", servers[2].url)
    assert result.returncode == 2
    assert "--strong-model-url" in result.stderr


def test_a_routed_chat_with_a_server_down_does_not_suggest_the_single_server_command(tmp_path, servers):
    stand_in = StandInServer()
    stand_in.close()
    result = chat(tmp_path, ["hello"], "--model-url", stand_in.url, "--strong-model-url", servers[1].url, "--router-url", servers[2].url)
    assert result.returncode == 1
    assert "llama-server -m" not in result.stderr
    assert "README" in result.stderr


def test_a_router_host_off_this_machine_stops_the_chat(tmp_path, servers):
    result = chat(tmp_path, ["hello"], "--model-url", servers[0].url, "--strong-model-url", servers[1].url, "--router-url", "http://example.com:8082")
    assert result.returncode == 2
    assert "this machine" in result.stderr
