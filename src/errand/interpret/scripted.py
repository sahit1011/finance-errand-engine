"""Scripted model for evals and offline demos. Its raw outputs go through the same parser as a live model's."""

from errand.interpret.model import ModelInput, ModelReply


class ScriptExhausted(Exception):
    pass


class ScriptedModel:
    name = "scripted"

    def __init__(self, outputs: list | None = None):
        self.outputs = list(outputs or [])
        self.inputs: list[ModelInput] = []

    def add(self, *outputs) -> None:
        self.outputs.extend(outputs)

    def decide(self, model_input: ModelInput, timeout_seconds: float) -> ModelReply:
        self.inputs.append(model_input)
        if not self.outputs:
            raise ScriptExhausted(f"no scripted output for message: {model_input.message!r}")
        output = self.outputs.pop(0)
        # A callable output can act on the world first, such as advancing a fake clock, then return raw output.
        if callable(output):
            output = output(model_input)
        return ModelReply(raw=output)


class LoopingModel:
    """Never finishes, so the model-call caps must stop it."""

    name = "looping"

    def __init__(self):
        self.inputs: list[ModelInput] = []

    def decide(self, model_input: ModelInput, timeout_seconds: float) -> ModelReply:
        self.inputs.append(model_input)
        return ModelReply(raw={"actions": [], "done": False})
