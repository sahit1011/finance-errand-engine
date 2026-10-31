"""Picks which model reads a message: nobody, the cheap LLM or the strong LLM. It can only pick a route.

Bare answers are settled in code before any model runs, so approval never reaches a router. A router sees only the
message and the state, so review text never reaches it either.
"""

import json
import math
from dataclasses import replace
from pathlib import Path

from errand.interpret.llama import HEALTH_TIMEOUT_SECONDS, LlamaModel
from errand.interpret.model import ModelInput, ModelReply

ROUTING_FILE = Path(__file__).parent / "route_examples.json"
NO_ACTION = '{"actions":[],"done":true}'
START = "START"
ROUTES = ("none", "cheap", "strong")


def route_text(state: str | None, message: str) -> str:
    return f"{state or START}: {message}"


def load_routing() -> dict:
    """The labelled examples and threshold that benchmarks/routing/benchmark.py chose from measured results."""
    routing = json.loads(ROUTING_FILE.read_text(encoding="utf-8"))
    for example in routing["examples"]:
        if example["route"] not in ROUTES:
            raise ValueError(f"unknown route {example['route']!r} in {ROUTING_FILE}")
    return routing


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


class RoutedModel:
    """Sends a message the way its nearest labelled example went. A weak match goes to the strong LLM."""

    name = "router"

    def __init__(self, cheap: LlamaModel, strong: LlamaModel, embedder: LlamaModel, routing: dict):
        self.cheap = cheap
        self.strong = strong
        self.embedder = embedder
        self.threshold = routing["threshold"]
        self.examples = routing["examples"]
        self.vectors: list[list[float]] = []

    def check(self) -> None:
        """Checks all three servers, and embeds the examples once so that each turn only embeds its own message."""
        self.cheap.check()
        self.strong.check()
        self.embedder.check()
        self.vectors = [self.embedder.embed(example["text"], HEALTH_TIMEOUT_SECONDS) for example in self.examples]

    def decide(self, model_input: ModelInput, timeout_seconds: float) -> ModelReply:
        route = self._route(model_input, timeout_seconds)
        if route == "none":
            return ModelReply(raw=NO_ACTION, served_by="router:none", prompt_tokens=0, completion_tokens=0, compute_seconds=0.0)
        reply = (self.cheap if route == "cheap" else self.strong).decide(model_input, timeout_seconds)
        return replace(reply, served_by=f"router:{route}")

    def _route(self, model_input: ModelInput, timeout_seconds: float) -> str:
        vector = self.embedder.embed(route_text(model_input.state, model_input.message), timeout_seconds)
        similarity, nearest = max(((_cosine(vector, other), index) for index, other in enumerate(self.vectors)), key=lambda pair: pair[0])
        return self.examples[nearest]["route"] if similarity >= self.threshold else "strong"
