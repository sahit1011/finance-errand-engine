"""Command line entry point: offline demos, and reading or following traces."""

import argparse
import json
import sys
import time
from pathlib import Path

from errand.demos import DEMOS, run_demo

DEFAULT_TRACE_DIR = "traces"
FOLLOW_POLL_SECONDS = 0.3


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


def main() -> int:
    parser = argparse.ArgumentParser(prog="errand")
    commands = parser.add_subparsers(dest="command", required=True)

    demo = commands.add_parser("demo", help="run an offline scripted demo")
    demo.add_argument("name", choices=sorted(DEMOS))
    demo.add_argument("--trace-dir", default=DEFAULT_TRACE_DIR)

    trace = commands.add_parser("trace", help="follow traces live, or print one run")
    trace.add_argument("run", nargs="?")
    trace.add_argument("--dir", default=DEFAULT_TRACE_DIR)

    args = parser.parse_args()
    if args.command == "demo":
        return run_demo(args.name, args.trace_dir)
    return _trace_command(args)
