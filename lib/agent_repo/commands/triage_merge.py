import copy
import json
import sys
from pathlib import Path

from agent_repo.config import ConfigError, load_config
from agent_repo.schema import load_schema, validate


def taxonomy(agents_dir: Path) -> set[str]:
    config = load_config(agents_dir)
    return (
        {label for label in config.labels if label.startswith("type:")}
        | {"area:" + area.name for area in config.areas}
        | {"area:unknown"}
    )


def label_corrections(tier2: dict, allowed_labels: set[str] | None) -> list[str] | None:
    corrections = list(
        dict.fromkeys(label for label in tier2["labels"] if label.startswith(("type:", "area:")))
    )
    if (
        allowed_labels is None
        or any(label not in allowed_labels for label in corrections)
        or sum(label.startswith("type:") for label in corrections) > 1
    ):
        return None
    return corrections


def llm_only_labels_match_answers(tier2: dict) -> bool:
    for question in ("type", "area"):
        answer = tier2["answers"][question]["value"]
        labels = [label for label in tier2["labels"] if label.startswith(f"{question}:")]
        if not isinstance(answer, str) or labels != [f"{question}:{answer}"]:
            return False
    return True


def merge(
    decision: dict, tier2: dict | None, error: str | None, *, allowed_labels: set[str] | None = None
) -> dict:
    """Without trusted taxonomy, type/area corrections retain Tier-1 fields and escalate.
    Matching or omitted type/area groups merge normally.
    """
    schema = load_schema("decision")
    if validate(decision, schema):
        raise ValueError("invalid Tier-1 decision")
    result = copy.deepcopy(decision)
    llm_only = decision.get("mode") == "llm-only"
    if llm_only and decision["answers"]:
        result["escalate"] = {
            "to": "maintainer",
            "reason": "LLM-only decision contains Tier-1 answers.",
        }
        return result
    if result["close"] is not None and not llm_only:
        result["close"]["tier2_agrees"] = False
    if (
        error is not None
        or tier2 is None
        or validate(tier2, schema)
        or any(tier2[key] != decision[key] for key in ["run_id", "base_sha", "schema_version"])
    ):
        if result["close"] is not None:
            result["close"]["tier2_agrees"] = False
        result["escalate"] = result["escalate"] or {
            "to": "maintainer",
            "reason": "Tier-2 triage failed or returned invalid output.",
        }
        return result
    if allowed_labels is None:
        unchanged = True
        for prefix in ("type:", "area:"):
            proposed = {label for label in tier2["labels"] if label.startswith(prefix)}
            current = {label for label in decision["labels"] if label.startswith(prefix)}
            if proposed and proposed != current:
                unchanged = False
        if unchanged:
            allowed_labels = set(decision["labels"])
    corrections = label_corrections(tier2, allowed_labels)
    if corrections is None:
        if result["close"] is not None:
            result["close"]["tier2_agrees"] = False
        result["escalate"] = result["escalate"] or {
            "to": "maintainer",
            "reason": "Tier-2 label corrections violate the repository taxonomy.",
        }
        return result
    if llm_only and (
        not {
            "type",
            "area",
            "priority",
            "needs_info",
            "out_of_scope",
            "docs_ref",
            "duplicate_of",
            "security_sensitive",
        }.issubset(tier2["answers"])
        or not llm_only_labels_match_answers(tier2)
    ):
        result["escalate"] = {
            "to": "maintainer",
            "reason": "LLM-only Tier-2 answers or labels are incomplete or inconsistent.",
        }
        return result
    for key in ["rca", "needs_info"]:
        result[key] = copy.deepcopy(tier2[key])
    result["escalate"] = copy.deepcopy(decision["escalate"] or tier2["escalate"])
    if llm_only:
        result["answers"] = copy.deepcopy(tier2["answers"])
        result["labels"] = [
            label for label in decision["labels"] if not label.startswith(("type:", "area:"))
        ]
        result["labels"] = list(dict.fromkeys(result["labels"] + tier2["labels"]))
        for key in ["suggestions", "close"]:
            result[key] = copy.deepcopy(tier2[key])
        if result["close"] is not None:
            result["close"]["tier2_agrees"] = False
        result["security"] = decision["security"] or tier2["security"]
        if validate(result, schema):
            raise ValueError("invalid merged decision")
        return result
    replaced = tuple(
        prefix for prefix in ["type:", "area:"] if any(c.startswith(prefix) for c in corrections)
    )
    result["labels"] = [label for label in decision["labels"] if not label.startswith(replaced)]
    result["labels"] = list(dict.fromkeys(result["labels"] + corrections))
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
    if len(argv) != 4:
        print(
            "usage: triage_merge <decision> <tier2-or-dash> <output> <trusted-agents-dir>",
            file=sys.stderr,
        )
        return 2
    try:
        decision = json.loads(Path(argv[0]).read_text())
        tier2 = json.loads(Path(argv[1]).read_text()) if argv[1] != "-" else None
        result = merge(decision, tier2, None, allowed_labels=taxonomy(Path(argv[3])))
        encoded = json.dumps(result, indent=2) + "\n"
        output = Path(argv[2])
        if not output.exists() or output.read_text() != encoded:
            output.write_text(encoded)
    except (OSError, ValueError, ConfigError):
        print("triage merge failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
