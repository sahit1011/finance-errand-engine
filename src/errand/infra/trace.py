"""One JSON Lines trace per run, with secrets removed and untrusted text marked."""

import json
import os
import re
import time
from pathlib import Path

SECRET_KEY_PATTERN = re.compile(r"(api[_-]?key|secret|token|password|authorization)", re.IGNORECASE)
SECRET_ENV_PATTERN = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD)", re.IGNORECASE)
REDACTED = "[REDACTED]"
# Long enough to recognise a review in a trace, short enough that a trace never carries it whole.
UNTRUSTED_PREVIEW_CHARS = 60
UNTRUSTED_FIELDS = {"reviews", "attributes"}
# Environment values this short are too common to redact safely, such as "1" or "true".
MIN_SECRET_VALUE_CHARS = 6
# Token-count keys are numbers about usage, not credentials, even though they contain "token".
USAGE_KEYS = {"prompt_tokens", "completion_tokens", "total_tokens"}


def sanitise(value):
    if isinstance(value, dict):
        return {k: _sanitise_field(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitise(item) for item in value]
    if isinstance(value, str):
        return _scrub_env_values(value)
    return value


def _sanitise_field(key: str, value):
    if key in USAGE_KEYS:
        return value
    if SECRET_KEY_PATTERN.search(key):
        return REDACTED
    if key in UNTRUSTED_FIELDS:
        return [_untrusted(item) for item in value]
    return sanitise(value)


def _untrusted(text: str) -> dict:
    return {"untrusted": True, "preview": _scrub_env_values(text[:UNTRUSTED_PREVIEW_CHARS]), "chars": len(text)}


def _scrub_env_values(text: str) -> str:
    for name, secret in os.environ.items():
        if SECRET_ENV_PATTERN.search(name) and len(secret) >= MIN_SECRET_VALUE_CHARS and secret in text:
            text = text.replace(secret, REDACTED)
    return text


class Tracer:
    """Events wait in memory until the turn's run is known, then go straight to that run's file."""

    def __init__(self, directory: str, clock):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._path: Path | None = None
        self._buffer: list[dict] = []

    def bind(self, run_id: str) -> None:
        self._path = self.directory / f"{run_id}.jsonl"
        buffered, self._buffer = self._buffer, []
        for event in buffered:
            self._write(event)

    def flush_unbound(self, name: str) -> None:
        """A turn that never joined a run, such as a memory command, still gets its own trace file."""
        if self._path is None and self._buffer:
            self.bind(name)

    def event(self, event_type: str, /, **fields) -> None:
        # "ns" orders events across files: "at" comes from the injected clock, which a test may hold still.
        event = {"at": self.clock.now().isoformat(), "ns": time.time_ns(), "type": event_type, **sanitise(fields)}
        if self._path is None:
            self._buffer.append(event)
        else:
            self._write(event)

    def _write(self, event: dict) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
