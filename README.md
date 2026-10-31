# Errand Agent

A chat agent that books, reschedules and cancels home-service errands against a mocked provider API. It never double-books, never acts without approval, and never obeys text inside a provider review. Code enforces all of that; a model only turns user text into structured data.

Everything runs on your machine. There are no cloud calls, and the tests need no model.

## Run it

You need [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest
uv run errand demo happy_path
```

`uv run pytest` runs every scenario offline and regenerates [eval_results.md](eval_results.md).

## Demos

These use the real engine and mock, with a scripted user and model.

| Command | What it shows |
| --- | --- |
| `uv run errand demo happy_path` | One full booking, then a repeated "yes" that does nothing |
| `uv run errand demo timeout_after_success` | `book` times out, but the booking happened, and the status check finds it |
| `uv run errand demo injection` | A review tells the agent to book, and a hijacked model tries to obey |

Each demo writes its trace to `traces/demo-<name>/`. To read one:

```sh
uv run errand trace run-1 --dir traces/demo-timeout_after_success
```

To watch traces live in a second terminal, run `uv run errand trace`.

## Not built yet

Live chat with a local model (`errand chat`), model routing, and the design note. See [the development plan](docs/DEVELOPMENT_PLAN.md).

## Documents

- [Product requirements](docs/PRD.md)
- [Specifications](docs/SPECS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Development plan](docs/DEVELOPMENT_PLAN.md)
- [Real bugs hit while building](BUGS.md)
