from collections.abc import Callable


def allow_only(allowed: list[str]) -> Callable[[dict], dict | None]:
    names = frozenset(allowed)

    def evaluate(event: dict) -> dict | None:
        if event.get("type") != "tool_call":
            return None
        target = event.get("target")
        if isinstance(target, str) and target in names:
            return {"result": "ALLOW"}
        return {"result": "DENY", "reason": "tool is not allowlisted"}

    return evaluate
