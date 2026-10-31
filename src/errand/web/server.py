"""The web app's HTTP server, using only the standard library. It listens on this machine and serves one user.

A page on any other website can send requests to a local server, so a plain server would let that page post "yes".
Every request therefore has its Host checked, and every POST its Origin and Content-Type, before anything else runs.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from errand.core.domain import ModelUnavailable
from errand.web import data
from errand.web.chat import PRESETS, Sessions, SessionChanged

LOCAL_HOST = "127.0.0.1"
STATIC_DIR = Path(__file__).parent / "static"
# Only these files are ever served from disk, so no request path can reach anything else.
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
# No outside scripts, styles, images or connections, so hostile text cannot load anything and nothing leaves this machine.
CONTENT_POLICY = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
# Far above any real message. Our choice.
MAX_BODY_BYTES = 64 * 1024
MAX_MESSAGE_CHARS = 2000
# How often a quiet event stream sends a comment, so the page notices if the server goes away.
KEEP_ALIVE_SECONDS = 15
# How long a request may take to arrive. Our choice: a page sends its whole request at once, so this only stops stuck clients.
REQUEST_TIMEOUT_SECONDS = 30
# Enough for any position in a session's steps.
MAX_POSITION_DIGITS = 12


class _App(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, state_root: Path, repo: Path, model_url: str, start_command: str):
        super().__init__((LOCAL_HOST, port), _Handler)
        self.repo = repo
        self.start_command = start_command
        self.request_timeout = REQUEST_TIMEOUT_SECONDS
        self.sessions = Sessions(state_root, model_url)
        actual_port = self.server_address[1]
        self.allowed_hosts = {f"{LOCAL_HOST}:{actual_port}", f"localhost:{actual_port}"}
        self.allowed_origins = {f"http://{host}" for host in self.allowed_hosts}

    def close_app(self) -> None:
        self.sessions.close()


class _Handler(BaseHTTPRequestHandler):
    server: _App

    def setup(self) -> None:
        self.timeout = self.server.request_timeout
        super().setup()

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        self._answering(self._get)

    def do_POST(self) -> None:
        self._answering(self._post)

    def _answering(self, work) -> None:
        """The one place that catches everything, because a request that fails must still get an answer that names why."""
        try:
            work()
        except (BrokenPipeError, ConnectionResetError):
            return  # The client went away, so there is nobody to answer.
        except Exception as error:
            self._json(500, {"error": f"{type(error).__name__}: {error}"})

    def _get(self) -> None:
        if not self._host_is_allowed():
            return
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if parts.path in STATIC_FILES:
            name, content_type = STATIC_FILES[parts.path]
            self._send(200, (STATIC_DIR / name).read_bytes(), content_type)
        elif parts.path == "/api/status":
            self._json(200, self._status())
        elif parts.path == "/api/state":
            self._json(200, self.server.sessions.state())
        elif parts.path == "/api/events":
            self._stream_events(query)
        elif parts.path == "/api/events-so-far":
            self._events_so_far(query)
        elif parts.path == "/data/results.json":
            self._json(200, data.load_results(self.server.repo))
        elif parts.path == "/data/replays.json":
            self._json(200, data.list_replays())
        elif parts.path.startswith("/data/replays/") and parts.path.endswith(".json"):
            replay = data.load_replay(self.server.repo, parts.path[len("/data/replays/") : -len(".json")])
            self._json(200, replay) if replay is not None else self._json(404, {"error": "no such replay"})
        else:
            self._json(404, {"error": "not found"})

    def _post(self) -> None:
        if not self._host_is_allowed():
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.server.allowed_origins:
            self._json(403, {"error": f"requests from {origin} are not allowed"})
            return
        # A page on another site can send a plain form without asking, but not JSON.
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "send application/json"})
            return
        length = self.headers.get("Content-Length")
        if length is None:
            self._json(411, {"error": "send a Content-Length"})
            return
        if not (length.isascii() and length.isdigit()):
            self._json(400, {"error": f"bad Content-Length: {length!r}"})
            return
        if int(length) > MAX_BODY_BYTES:
            self._json(413, {"error": f"the body is over {MAX_BODY_BYTES} bytes"})
            return
        try:
            raw = self.rfile.read(int(length))
        except TimeoutError:
            self._json(408, {"error": "the body did not arrive in time"})
            return
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as error:
            self._json(400, {"error": f"the body is not JSON: {error}"})
            return
        if not isinstance(body, dict):
            self._json(400, {"error": "the body must be a JSON object"})
            return
        if self.path == "/api/message":
            self._message(body)
        elif self.path == "/api/session":
            self._new_session(body)
        else:
            self._json(404, {"error": "not found"})

    def _host_is_allowed(self) -> bool:
        hosts = self.headers.get_all("Host", [])
        if len(hosts) == 1 and hosts[0] in self.server.allowed_hosts:
            return True
        self._json(403, {"error": f"this app does not answer to the host {hosts}"})
        return False

    def _status(self) -> dict:
        try:
            self.server.sessions.model.check()
        except ModelUnavailable as error:
            return {"model_ok": False, "problem": str(error), "start_command": self.server.start_command}
        return {"model_ok": True, "problem": None, "start_command": self.server.start_command}

    def _message(self, body: dict) -> None:
        session = body.get("session")
        if not isinstance(session, str):
            self._json(400, {"error": "session must say which session this page is showing"})
            return
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            self._json(400, {"error": "text must be a non-empty string"})
            return
        if len(text) > MAX_MESSAGE_CHARS:
            self._json(400, {"error": f"text is over {MAX_MESSAGE_CHARS} characters"})
            return
        last_reply_id = body.get("last_reply_id")
        if last_reply_id is not None and (not isinstance(last_reply_id, int) or isinstance(last_reply_id, bool)):
            self._json(400, {"error": "last_reply_id must be a number or null"})
            return
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            self._json(400, {"error": "text holds characters that cannot be stored"})
            return
        try:
            state = self.server.sessions.say(session, text.strip(), last_reply_id)
        except SessionChanged as changed:
            self._json(409, {"error": "another tab started a newer session, so this was not acted on", "session": str(changed)})
            return
        self._json(200, {**state, "text": state["messages"][-1]["text"]})

    def _new_session(self, body: dict) -> None:
        preset = body.get("preset")
        if not isinstance(preset, str) or preset not in PRESETS:
            self._json(400, {"error": f"unknown preset {preset!r}, expected one of {sorted(PRESETS)}"})
            return
        self._json(200, self.server.sessions.start_preset(preset))

    def _log_and_position(self, query: dict) -> tuple | None:
        """Answers the request itself and returns None if the session or position is not usable."""
        log = self.server.sessions.log_for(query.get("session", [""])[0])
        if log is None:
            self._json(404, {"error": "no such session"})
            return None
        after = query.get("after", ["0"])[0]
        if not (after.isascii() and after.isdigit() and len(after) <= MAX_POSITION_DIGITS):
            self._json(400, {"error": f"after must be a number, not {after!r}"})
            return None
        return log, int(after)

    def _events_so_far(self, query: dict) -> None:
        found = self._log_and_position(query)
        if found is not None:
            log, position = found
            self._json(200, {"events": log.after(position)})

    def _stream_events(self, query: dict) -> None:
        found = self._log_and_position(query)
        if found is None:
            return
        log, position = found
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                events = log.after(position, KEEP_ALIVE_SECONDS)
                for event in events:
                    self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
                if not events:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                position += len(events)
        except (BrokenPipeError, ConnectionResetError):
            return  # The page closed or moved to another session.
        except TimeoutError:
            return  # The page stopped reading, so this stream is over.

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CONTENT_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def make_server(state_root: Path, repo: Path, model_url: str, port: int, start_command: str = "llama-server") -> _App:
    return _App(port, state_root, repo, model_url, start_command)
