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

Chat needs macOS or Linux, llama.cpp, and one 5.68 GB model file ([Qwen3.5 9B](https://huggingface.co/Qwen/Qwen3.5-9B), Apache 2.0). Your messages stay on this machine: the agent refuses a model server anywhere else.

```sh
brew install llama.cpp
mkdir -p ~/models/qwen3.5-9b
curl -L -o ~/models/qwen3.5-9b/Qwen3.5-9B-Q4_K_M.gguf https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-Q4_K_M.gguf
```

Then use three terminals:

```sh
# 1. The model server
llama-server -m ~/models/qwen3.5-9b/Qwen3.5-9B-Q4_K_M.gguf --host 127.0.0.1 --port 8080 --ctx-size 8192 --parallel 1 --no-webui

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

## Model routing

Routing sends each message to no LLM, a cheap LLM or a strong one. We measured it and recommend the single 9B server above: the router scored below either model alone on held-out turns, and 9B made the fewest silent mistakes and returned no false stops, where 4B returned 3. See [the results](benchmarks/routing/results.md). To try it, also download [Qwen3.5 4B](https://huggingface.co/unsloth/Qwen3.5-4B-GGUF) and [EmbeddingGemma 2](https://huggingface.co/ggml-org/embeddinggemma-2-GGUF), run a server for each on ports 8080 (4B), 8081 (9B) and 8082 (EmbeddingGemma, with `--embeddings`), then:

```sh
uv run errand chat --model-url http://127.0.0.1:8080 --strong-model-url http://127.0.0.1:8081 --router-url http://127.0.0.1:8082
```

## Documents

- [Design note](DESIGN.md)
- [A raw trace with a timeout after success](docs/trace-timeout-after-success.jsonl)
- [Eval table](eval_results.md)
- [Model routing results](benchmarks/routing/results.md)
- [Product requirements](docs/PRD.md)
- [Specifications](docs/SPECS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Development plan](docs/DEVELOPMENT_PLAN.md)
- [Real bugs hit while building](BUGS.md)
