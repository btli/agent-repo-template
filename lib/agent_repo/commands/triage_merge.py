import copy
import json
import sys
from pathlib import Path

from agent_repo.schema import load_schema, validate


def merge(decision: dict, tier2: dict | None, error: str | None) -> dict:
    schema = load_schema("decision")
    if validate(decision, schema):
        raise ValueError("invalid Tier-1 decision")
    result = copy.deepcopy(decision)
    if (
        error is not None
        or tier2 is None
        or validate(tier2, schema)
        or any(tier2[key] != decision[key] for key in ["run_id", "base_sha", "schema_version"])
    ):
        result["escalate"] = {
            "to": "maintainer",
            "reason": "Tier-2 triage failed or returned invalid output.",
        }
        return result
    for key in ["rca", "needs_info", "escalate"]:
        result[key] = copy.deepcopy(tier2[key])
    result["labels"] = [
        label for label in decision["labels"] if not label.startswith(("type:", "area:"))
    ]
    result["labels"].extend(
        label for label in tier2["labels"] if label.startswith(("type:", "area:"))
    )
    if result["close"] is not None:
        vote = tier2["close"]
        result["close"]["tier2_agrees"] = bool(
            vote
            and all(vote[key] == result["close"][key] for key in ["kind", "target", "docs_ref"])
            and vote["tier2_agrees"]
        )
    if validate(result, schema):
        raise ValueError("invalid merged decision")
    return result


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: triage_merge <decision> <tier2-or-dash> <output>", file=sys.stderr)
        return 2
    try:
        decision = json.loads(Path(argv[0]).read_text())
        tier2 = json.loads(Path(argv[1]).read_text()) if argv[1] != "-" else None
        result = merge(decision, tier2, None)
        encoded = json.dumps(result, indent=2) + "\n"
        output = Path(argv[2])
        if not output.exists() or output.read_text() != encoded:
            output.write_text(encoded)
    except (OSError, ValueError):
        print("triage merge failed", file=sys.stderr)
        return 1
    return 0
