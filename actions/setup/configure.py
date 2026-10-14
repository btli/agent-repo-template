import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


def main() -> int:
    try:
        url = os.environ["GATEWAY_URL"].rstrip("/")
        kind = os.environ["GATEWAY_KIND"]
        model = os.environ["MODEL"]
        key_env = os.environ["API_KEY_ENV"]
        parsed = urlsplit(url)
        if (
            kind not in {"databricks", "litellm"}
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or re.fullmatch(r"[A-Z_][A-Z0-9_]*", key_env) is None
            or re.fullmatch(r"(?:[A-Za-z0-9_-]+\.)*claude[-.a-zA-Z0-9_]+", model) is None
        ):
            raise ValueError("invalid gateway configuration")
        base = url + "/anthropic" if kind == "databricks" else url
        config = {
            "providers": {
                "ci-gateway": {
                    "kind": "gateway",
                    "default": ["anthropic"],
                    "anthropic": {
                        "base_url": base,
                        "api_key_ref": "env:LLM_API_KEY",
                        "models": {"default": model},
                    },
                }
            }
        }
        path = Path.home() / ".omnigent/config.yaml"
        encoded = json.dumps(config, indent=2) + "\n"
        if not path.exists() or path.read_text() != encoded:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(encoded)
    except (KeyError, OSError, ValueError):
        print("invalid gateway configuration", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
