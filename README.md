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

## Chat with a local model

Chat needs macOS or Linux, llama.cpp, and one 2.74 GB model file ([Qwen3.5 4B](https://huggingface.co/Qwen/Qwen3.5-4B), Apache 2.0). Your messages stay on this machine: the agent refuses a model server anywhere else.

```sh
brew install llama.cpp
mkdir -p ~/models/qwen3.5-4b
curl -L -o ~/models/qwen3.5-4b/Qwen3.5-4B-Q4_K_M.gguf https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/main/Qwen3.5-4B-Q4_K_M.gguf
```

Then use three terminals:

```sh
# 1. The model server
llama-server -m ~/models/qwen3.5-4b/Qwen3.5-4B-Q4_K_M.gguf --host 127.0.0.1 --port 8080 --ctx-size 8192 --parallel 1 --no-webui

# 2. The chat. Ctrl-D leaves, and your errand and memory are saved.
uv run errand chat

# 3. The live trace
uv run errand trace --dir traces/default
```

To see a failure live, force the outcome of the next `book` call in a new session:

```sh
uv run errand chat --session failure --book-outcome timeout_booked
uv run errand trace --dir traces/failure
```

The outcomes are `success`, `slot_taken`, `timeout_booked` and `timeout_not_booked`. Each session keeps its state in `.errand/<session>/` and its traces in `traces/<session>/`.

## Not built yet

Model routing and the design note. See [the development plan](docs/DEVELOPMENT_PLAN.md).

## Documents

- [Product requirements](docs/PRD.md)
- [Specifications](docs/SPECS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Development plan](docs/DEVELOPMENT_PLAN.md)
- [Real bugs hit while building](BUGS.md)
