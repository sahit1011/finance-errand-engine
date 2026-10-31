"""Names and failure types of the fixed tool contract, shared by the mock and the gateway."""

SERVICES = ("plumber", "electrician", "ac_service", "carpenter", "cleaner")
AREAS = ("indiranagar", "koramangala", "whitefield", "jayanagar")


class ToolError(Exception):
    """A failure the contract names, such as slot_unavailable."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class ToolTimeout(ToolError):
    """The call timed out, and the write may or may not have happened."""

    def __init__(self, detail: str = ""):
        super().__init__("timeout", detail)


class TransientError(ToolError):
    """A retry with the same idempotency key is expected to succeed."""

    def __init__(self, detail: str = ""):
        super().__init__("503", detail)
