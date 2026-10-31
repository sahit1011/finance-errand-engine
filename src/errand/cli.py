"""Command line entry point: live chat, offline demos, and reading or following traces."""

import argparse
import json
import re
import select
import sys
import time
from pathlib import Path

from errand.core.domain import ModelUnavailable
from errand.demos import DEMOS, run_demo
from errand.infra.clock import SystemClock
from errand.infra.store import Store
from errand.interpret.llama import LlamaModel
from errand.mock.world import OUTCOME_SLOT_TAKEN, OUTCOME_SUCCESS, OUTCOME_TIMEOUT_BOOKED, OUTCOME_TIMEOUT_NOT_BOOKED, MockWorld, Scenario
from errand.runner import Engine

DEFAULT_TRACE_DIR = "traces"
FOLLOW_POLL_SECONDS = 0.3
DATA_ROOT = Path(".errand")
CHAT_USER = "user-1"
# Uses the port in LLAMA_SERVER_COMMAND below, which the README repeats.
DEFAULT_MODEL_URL = "http://127.0.0.1:8080"
LLAMA_SERVER_COMMAND = "llama-server -m ~/models/qwen3.5-4b/Qwen3.5-4B-Q4_K_M.gguf --host 127.0.0.1 --port 8080 --ctx-size 8192 --parallel 1 --no-webui"
BOOK_OUTCOMES = (OUTCOME_SUCCESS, OUTCOME_SLOT_TAKEN, OUTCOME_TIMEOUT_BOOKED, OUTCOME_TIMEOUT_NOT_BOOKED)


def _print_event(line: str) -> None:
    event = json.loads(line)
    kind = event.pop("type")
    event.pop("at", None)
    event.pop("ns", None)
    if kind == "transition":
        print(f"[state]  {event['from_state']} -> {event['to_state']}  ({event['reason']})")
    elif kind == "tool_call":
        outcome = f"error={event['error']}" if "error" in event else "ok"
        print(f"[tool]   {event['tool']}{tuple(event['args'])} {outcome} {event['latency_ms']}ms")
    elif kind == "model_call":
        print(f"[model]  {event['model']} {event['latency_ms']}ms tokens={event['prompt_tokens']}/{event['completion_tokens']}")
    elif kind == "approval":
        print(f"[approval] {event['approval_id']} {event['status']}")
    else:
        print(f"[{kind}] " + json.dumps(event, ensure_ascii=False)[:160])


def _trace_command(args: argparse.Namespace) -> int:
    directory = Path(args.dir)
    if args.run:
        path = directory / f"{args.run}.jsonl"
        if not path.exists():
            print(f"No trace at {path}", file=sys.stderr)
            return 1
        for line in path.read_text(encoding="utf-8").splitlines():
            _print_event(line)
        return 0
    positions: dict[Path, int] = {}
    print(f"Following traces in {directory}/ (Ctrl-C to stop)")
    try:
        while True:
            for path in sorted(directory.rglob("*.jsonl")):
                with path.open(encoding="utf-8") as handle:
                    handle.seek(positions.get(path, 0))
                    for line in handle:
                        _print_event(line)
                    positions[path] = handle.tell()
            time.sleep(FOLLOW_POLL_SECONDS)
    except KeyboardInterrupt:
        return 0


def _session_name(text: str) -> str:
    if not re.fullmatch(r"[\w-]+", text):
        raise argparse.ArgumentTypeError(f"use only letters, numbers, - and _, not {text!r}")
    return text


def _discard_typed_ahead() -> bool:
    """Text typed while the agent worked was typed before its last reply was seen, so it must not count as an answer to it."""
    if not sys.stdin.isatty():
        return False
    # Here because only chat needs it, and it exists only on macOS and Linux.
    import termios

    pending = bool(select.select([sys.stdin], [], [], 0)[0])
    termios.tcflush(sys.stdin, termios.TCIFLUSH)
    return pending


def _chat_command(args: argparse.Namespace) -> int:
    try:
        model = LlamaModel(args.model_url)
    except ValueError as error:
        print(f"Cannot start: {error}", file=sys.stderr)
        return 2
    try:
        model.check()
    except ModelUnavailable as error:
        print(f"Cannot start: {error}\nStart the model server first:\n  {LLAMA_SERVER_COMMAND}", file=sys.stderr)
        return 1
    # Each session has its own run numbers, so its traces need their own folder too.
    data_dir = DATA_ROOT / args.session
    world_path = data_dir / "world.sqlite"
    if args.book_outcome and world_path.exists():
        print(f"Cannot start: --book-outcome needs a new session, because session {args.session!r} has already counted its book calls, so the outcome would be ignored.", file=sys.stderr)
        return 2
    data_dir.mkdir(parents=True, exist_ok=True)
    trace_dir = Path(DEFAULT_TRACE_DIR) / args.session
    trace_dir.mkdir(parents=True, exist_ok=True)
    clock = SystemClock()
    world = MockWorld(str(world_path), Scenario(user_id=CHAT_USER, book_outcomes=args.book_outcome), clock)
    store = Store(str(data_dir / "engine.sqlite"), clock)
    engine = Engine(world, store, model, str(trace_dir), clock)
    print(f"Session {args.session}, traces in {trace_dir}/. Press Ctrl-D to leave; your errand and memory are saved.", flush=True)
    last_reply_id = None
    try:
        for reply in engine.start_up(CHAT_USER):
            print(f"agent> {reply.text}\n", flush=True)
            last_reply_id = reply.id
        while True:
            if _discard_typed_ahead():
                print("(I ignored what you typed while I was working, because you had not seen this yet.)\n", flush=True)
            text = input("you> ").strip()
            if not text:
                continue
            reply = engine.handle_message(CHAT_USER, text, last_reply_id)
            last_reply_id = reply.id
            print(f"agent> {reply.text}\n", flush=True)
    except (EOFError, KeyboardInterrupt):
        print()
        return 0
    finally:
        store.close()
        world.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="errand")
    commands = parser.add_subparsers(dest="command", required=True)

    chat = commands.add_parser("chat", help="chat with the agent, using a local model server")
    chat.add_argument("--model-url", default=DEFAULT_MODEL_URL)
    chat.add_argument("--session", type=_session_name, default="default", help="a name that keeps this session's state and traces apart")
    chat.add_argument("--book-outcome", action="append", default=[], choices=BOOK_OUTCOMES, help="force the next book call's outcome, for showing a failure live; needs a new session")

    demo = commands.add_parser("demo", help="run an offline scripted demo")
    demo.add_argument("name", choices=sorted(DEMOS))
    demo.add_argument("--trace-dir", default=DEFAULT_TRACE_DIR)

    trace = commands.add_parser("trace", help="follow traces live, or print one run")
    trace.add_argument("run", nargs="?")
    trace.add_argument("--dir", default=DEFAULT_TRACE_DIR)

    args = parser.parse_args()
    if args.command == "chat":
        return _chat_command(args)
    if args.command == "demo":
        return run_demo(args.name, args.trace_dir)
    return _trace_command(args)
