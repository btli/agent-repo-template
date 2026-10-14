from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
    from agent_repo.systemone import ProviderConfig, Question, SystemOneError, ask

    required = ("SYSTEMONE_PROVIDER", "SYSTEMONE_BASE_URL", "SYSTEMONE_MODEL", "SYSTEMONE_API_KEY")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print(f"missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        return 2

    config = ProviderConfig(
        provider=os.environ["SYSTEMONE_PROVIDER"],
        model=os.environ["SYSTEMONE_MODEL"],
    )
    try:
        answers = ask(
            config,
            os.environ["SYSTEMONE_BASE_URL"],
            os.environ["SYSTEMONE_API_KEY"],
            os.environ.get("SYSTEMONE_INPUT", "A customer says they were charged twice."),
            [
                Question(
                    id="smoke",
                    kind="noul",
                    prompt="Is this message about billing?",
                    options=None,
                )
            ],
        )
    except SystemOneError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    answer = answers["smoke"]
    print(json.dumps(asdict(answer) if answer is not None else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
