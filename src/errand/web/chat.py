"""The live chat behind the web app: one session at a time, with every engine step shown as it happens."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Condition, Lock

from errand.infra.clock import SystemClock
from errand.infra.store import Store
from errand.interpret.llama import LlamaModel
from errand.mock.world import OUTCOME_SLOT_TAKEN, OUTCOME_TIMEOUT_BOOKED, OUTCOME_TIMEOUT_NOT_BOOKED, MockWorld, Scenario
from errand.runner import Engine

USER = "user-1"
DEFAULT_SESSION = "web"
# What the page offers for starting a session. Each forces the first book call, so a failure can be shown on demand.
PRESETS = {
    "normal": [],
    "timeout_after_success": [OUTCOME_TIMEOUT_BOOKED],
    "timeout_not_booked": [OUTCOME_TIMEOUT_NOT_BOOKED],
    "slot_taken": [OUTCOME_SLOT_TAKEN],
}


class SessionChanged(Exception):
    """A message was for a session that is no longer the current one."""


class EventLog:
    """Engine steps in order, which any number of readers can follow as they arrive."""

    def __init__(self, session: str):
        self.session = session
        self._events: list[dict] = []
        self._changed = Condition()

    def add(self, event: dict) -> None:
        with self._changed:
            self._events.append({**event, "seq": len(self._events) + 1})
            self._changed.notify_all()

    def after(self, position: int, wait_seconds: float = 0) -> list[dict]:
        with self._changed:
            self._changed.wait_for(lambda: len(self._events) > position, timeout=wait_seconds)
            return self._events[position:]


def _saved_events(trace_dir: Path) -> list[dict]:
    """Steps from earlier runs of this session, in the order they happened across its trace files.

    A process killed mid-write can leave a line cut off. That costs one step of history, so it shows as a note and
    must not stop a session that has to resume after exactly such a crash.
    """
    events = []
    for path in trace_dir.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"at": "", "ns": path.stat().st_mtime_ns, "type": "error", "where": "trace", "detail": f"a line in {path.name} was cut off, so one step is missing here"})
    return sorted(events, key=lambda event: event["ns"])


class Session:
    def __init__(self, name: str, preset: str, state_root: Path, model):
        self.name = name
        self.preset = preset
        self.messages: list[dict] = []
        self.last_reply_id: int | None = None
        self.log = EventLog(name)
        data_dir = state_root / ".errand" / name
        data_dir.mkdir(parents=True, exist_ok=True)
        trace_dir = state_root / "traces" / name
        trace_dir.mkdir(parents=True, exist_ok=True)
        for event in _saved_events(trace_dir):
            self.log.add(event)
        clock = SystemClock()
        self.world = MockWorld(str(data_dir / "world.sqlite"), Scenario(user_id=USER, book_outcomes=PRESETS[preset]), clock)
        self.store = Store(str(data_dir / "engine.sqlite"), clock)
        self.engine = Engine(self.world, self.store, model, str(trace_dir), clock, on_event=self.log.add)
        for reply in self.engine.start_up(USER):
            self._show_reply(reply)
        self._save_state()

    def say(self, text: str, last_reply_id: int | None) -> None:
        reply = self.engine.handle_message(USER, text, last_reply_id)
        self.messages.append({"role": "user", "text": text})
        self._show_reply(reply)
        self._save_state()

    def _save_state(self) -> None:
        """Kept ready so a page can load during a long turn, when the engine's thread is busy and the store is out of reach."""
        run = self.store.latest_run(USER)
        self.state = {
            "session": self.name,
            "preset": self.preset,
            "messages": list(self.messages),
            "last_reply_id": self.last_reply_id,
            "run_state": run.state.value if run else None,
        }

    def close(self) -> None:
        self.store.close()
        self.world.close()

    def _show_reply(self, reply) -> None:
        self.messages.append({"role": "agent", "text": reply.text, "id": reply.id})
        self.last_reply_id = reply.id


class Sessions:
    """Runs all engine work on one thread, because a SQLite connection belongs to the thread that opened it.

    One thread also means turns run strictly in order, so a queued yes is judged after the turn before it, and the
    engine's stale check then rejects it if a new proposal has appeared since.
    """

    def __init__(self, state_root: Path, model_url: str):
        self.state_root = state_root
        self.model = LlamaModel(model_url)
        self._worker = ThreadPoolExecutor(max_workers=1)
        self._current: Session | None = None
        self._turns_running = 0
        self._turns_lock = Lock()
        self.open_new(DEFAULT_SESSION, "normal")

    def open_new(self, name: str, preset: str) -> dict:
        return self._on_worker(lambda: self._switch(name, preset))

    def start_preset(self, preset: str) -> dict:
        return self.open_new(self._unused_name(), preset)

    def say(self, session: str, text: str, last_reply_id: int | None) -> dict:
        """Reply ids restart in each session, so a page's id only means something in the session that page was showing."""

        def turn() -> None:
            if self._current.name != session:
                raise SessionChanged(self._current.name)
            self._current.say(text, last_reply_id)

        with self._turns_lock:
            self._turns_running += 1
        try:
            self._on_worker(turn)
        finally:
            with self._turns_lock:
                self._turns_running -= 1
        return self.state()

    def state(self) -> dict:
        with self._turns_lock:
            busy = self._turns_running > 0
        return {**self._current.state, "busy": busy}

    def log_for(self, session: str) -> EventLog | None:
        current = self._current
        return current.log if current is not None and current.name == session else None

    def close(self) -> None:
        self._on_worker(lambda: self._current.close())
        self._worker.shutdown()

    def _on_worker(self, work):
        return self._worker.submit(work).result()

    def _switch(self, name: str, preset: str) -> dict:
        # The new session opens first, so a failure leaves the current one working.
        opened = Session(name, preset, self.state_root, self.model)
        if self._current is not None:
            self._current.close()
        self._current = opened
        return self.state()

    def _unused_name(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name, number = f"web-{stamp}", 1
        while (self.state_root / ".errand" / name).exists():
            number += 1
            name = f"web-{stamp}-{number}"
        return name
