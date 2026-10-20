import argparse
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"^[0-9a-f]{40}$")
VERSION = re.compile(r"^v\d+\.\d+\.\d+$")


def render_consumer(
    template_sha: str, template_version: str, *, temporary_parent: Path | None = None
) -> Path:
    if not SHA.fullmatch(template_sha):
        raise ValueError("template SHA must contain 40 lowercase hexadecimal characters")
    if not VERSION.fullmatch(template_version):
        raise ValueError("template version must use vX.Y.Z form")

    temporary_root = Path(tempfile.gettempdir()).resolve()
    temporary_parent = temporary_parent.resolve() if temporary_parent else temporary_root
    if not temporary_parent.is_relative_to(temporary_root):
        raise ValueError("render destination must be inside the system temporary directory")
    output = Path(tempfile.mkdtemp(prefix="agent-repo-consumer-", dir=temporary_parent))
    source = ROOT / "consumer"
    destination = output / "consumer"
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        content = path.read_text(encoding="utf-8")
        content = content.replace("__TEMPLATE_SHA__", template_sha)
        content = content.replace("__TEMPLATE_VERSION__", template_version)
        target.write_text(content, encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Render consumer files for workflow linting.")
    parser.add_argument("--template-sha", required=True)
    parser.add_argument("--template-version", required=True)
    args = parser.parse_args()
    print(render_consumer(args.template_sha, args.template_version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
