"""The live model client, run over real HTTP against a stand-in server. No model is involved."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest

from datetime import date

from errand.core.domain import ModelUnavailable
from errand.core.when import WHEN_WORDS, resolve_when
from errand.interpret.llama import LlamaModel, output_schema
from errand.interpret.model import ACTION_FIELDS, DETAIL_KEYS, ModelInput, parse_output

GOOD_BODY = {
    "choices": [{"message": {"content": '{"actions": [], "done": true}'}}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    "timings": {"prompt_ms": 100.0, "predicted_ms": 400.0},
}


class StandInServer:
    """Answers like llama-server, with whatever status and body a test sets."""

    def __init__(self):
        self.status = 200
        self.body: object = GOOD_BODY
        self.requests: list[dict] = []
        self.hang = Event()
        self.release = Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                if outer.hang.is_set():
                    outer.release.wait(5)
                try:
                    self._answer(outer.requests[-1])
                except (BrokenPipeError, ConnectionResetError):
                    pass  # The client gave up waiting, which is what a hang test is about.

            def do_GET(self):
                self._answer()

            def _answer(self, request=None):
                # A callable body answers each request by its content, and a health check gets a plain ok.
                answer = (outer.body(request) if request else {"status": "ok"}) if callable(outer.body) else outer.body
                body = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def server():
    stand_in = StandInServer()
    yield stand_in
    stand_in.close()


def closed_port_url() -> str:
    stand_in = StandInServer()
    stand_in.close()
    return stand_in.url


@pytest.mark.parametrize("url", ["http://example.com:8080", "http://10.0.0.5:8080", "http://8.8.8.8", "https://127.0.0.1:8080", "ftp://localhost", "not a url"])
def test_a_model_host_off_this_machine_is_refused_before_any_connection(url):
    with pytest.raises(ValueError, match="this machine"):
        LlamaModel(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080", "http://localhost:8080", "http://[::1]:8080"])
def test_a_host_on_this_machine_is_accepted(url):
    LlamaModel(url)


def test_a_proxy_setting_cannot_send_a_message_off_this_machine(server, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    LlamaModel(server.url).decide(ModelInput(message="hi", today="2026-11-02"), 5)
    assert len(server.requests) == 1


def test_a_reply_carries_the_output_tokens_and_compute_time(server):
    reply = LlamaModel(server.url).decide(ModelInput(message="hi", today="2026-11-02"), 5)
    assert reply.raw == '{"actions": [], "done": true}'
    assert (reply.prompt_tokens, reply.completion_tokens) == (11, 7)
    assert reply.compute_seconds == pytest.approx(0.5)


def test_the_request_constrains_output_turns_thinking_off_and_sends_the_engine_input(server):
    LlamaModel(server.url).decide(ModelInput(message="Find a plumber", today="2026-11-02", state="COLLECTING"), 5)
    request = server.requests[0]
    assert request["response_format"]["json_schema"]["schema"] == output_schema()
    assert request["chat_template_kwargs"] == {"enable_thinking": False}
    sent = json.loads(request["messages"][-1]["content"])
    assert sent["message"] == "Find a plumber" and sent["state"] == "COLLECTING"


def test_the_schema_offers_only_engine_actions_and_nothing_that_writes():
    variants = output_schema()["properties"]["actions"]["items"]["anyOf"]
    offered = {variant["properties"]["type"]["const"] for variant in variants}
    assert offered <= set(ACTION_FIELDS)
    assert offered.isdisjoint({"book", "cancel", "reschedule", "hold_slot", "approve"})


@pytest.mark.parametrize(
    "output",
    [
        {"actions": [{"type": "set_intent", "intent": "book"}], "done": True},
        {"actions": [{"type": "update_details", "details": {"budget": "no_limit"}}, {"type": "update_details", "details": {"latest_time": "12:00"}}], "done": True},
        {"actions": [{"type": "select_option", "option": 2}], "done": True},
        {"actions": [{"type": "request_read", "tool": "get_provider", "option": 1}], "done": True},
        {"actions": [{"type": "propose_memory", "key": "area", "value": "whitefield"}], "done": True},
        {"actions": [{"type": "need_clarification"}], "done": True},
    ],
)
def test_each_shape_the_schema_describes_is_one_the_engine_parser_accepts(output):
    variants = output_schema()["properties"]["actions"]["items"]["anyOf"]
    kind = output["actions"][0]["type"]
    variant = next(v for v in variants if v["properties"]["type"]["const"] == kind)
    assert set(output["actions"][0]) <= set(variant["properties"])
    assert parse_output(output).actions == output["actions"]


def test_a_live_model_cannot_ask_for_a_second_step_on_one_message():
    """Nothing new reaches it on a second step, so a real model repeats itself until a cap stops it."""
    assert output_schema()["properties"]["done"] == {"const": True}


def detail_variants() -> list[dict]:
    update = next(v for v in output_schema()["properties"]["actions"]["items"]["anyOf"] if v["properties"]["type"]["const"] == "update_details")
    return update["properties"]["details"]["anyOf"]


def test_the_schema_cannot_force_a_detail_the_user_never_gave():
    """llama.cpp's grammar fixes key order, so several optional keys in one object can leave only one legal next key."""
    variants = detail_variants()
    assert {key for variant in variants for key in variant["properties"]} == set(DETAIL_KEYS)
    assert all(list(variant["properties"]) == variant["required"] and len(variant["required"]) == 1 for variant in variants)


def test_every_time_word_the_schema_offers_is_one_the_engine_can_resolve():
    when = next(variant["properties"]["when"] for variant in detail_variants() if "when" in variant["properties"])
    words = next(option["enum"] for option in when["anyOf"] if "enum" in option)
    assert set(words) == set(WHEN_WORDS)
    for word in words:
        resolve_when(word, date(2026, 11, 2))


def test_a_server_that_is_down_is_a_named_failure():
    url = closed_port_url()
    with pytest.raises(ModelUnavailable, match=url.split("//")[1]):
        LlamaModel(url).decide(ModelInput(message="hi", today="2026-11-02"), 5)


def test_a_slow_model_cannot_outlast_the_time_it_was_given(server):
    server.hang.set()
    with pytest.raises(ModelUnavailable, match="0.2"):
        LlamaModel(server.url).decide(ModelInput(message="hi", today="2026-11-02"), 0.2)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (500, {"error": "boom"}, "500"),
        (503, {"error": {"message": "Loading model"}}, "503"),
        (200, b"not json", "not JSON"),
        (200, {"choices": []}, "unexpected"),
        (200, {"choices": [{"message": {}}], "usage": {}, "timings": {}}, "unexpected"),
    ],
)
def test_a_bad_server_response_is_a_named_failure_rather_than_a_guess(server, status, body, expected):
    server.status, server.body = status, body
    with pytest.raises(ModelUnavailable, match=expected):
        LlamaModel(server.url).decide(ModelInput(message="hi", today="2026-11-02"), 5)


def test_a_redirect_is_not_followed_off_this_machine(server):
    server.status, server.body = 302, {}
    with pytest.raises(ModelUnavailable, match="302"):
        LlamaModel(server.url).decide(ModelInput(message="hi", today="2026-11-02"), 5)
    assert len(server.requests) == 1


def test_check_passes_when_the_server_is_healthy(server):
    server.body = {"status": "ok"}
    LlamaModel(server.url).check()


def test_check_names_a_server_still_loading(server):
    server.status, server.body = 503, {"error": {"message": "Loading model"}}
    with pytest.raises(ModelUnavailable, match="503"):
        LlamaModel(server.url).check()


def test_check_names_a_server_that_is_down():
    with pytest.raises(ModelUnavailable):
        LlamaModel(closed_port_url()).check()
