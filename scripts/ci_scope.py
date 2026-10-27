"""Select independent CI checks from the complete Git diff; unknown changes run everything."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

SCOPES = ("backend", "frontend", "browser")
ROOT = Path(__file__).resolve().parents[1]


def classify_changes(paths):
    selected = set()
    for path in paths:
        if (
            path.startswith(("docs/", ".impeccable/"))
            or ("/" not in path and path.endswith(".md"))
            or path == "LICENSE"
        ):
            continue
        if path.startswith("frontend/console/"):
            selected.update(("frontend", "browser"))
        elif path.startswith(("backend/", "tests/")):
            selected.update(("backend", "browser"))
        else:
            return set(SCOPES)
    return selected


def changed_files(base, head, *, pull_request=False, cwd=ROOT):
    if not all(re.fullmatch(r"[0-9a-f]{40}", value or "") for value in (base, head)):
        raise ValueError("Git comparison needs full commit SHAs")
    if base == "0" * 40:
        raise ValueError("Initial push has no comparison base")
    comparison = f"{base}{'...' if pull_request else '..'}{head}"
    # Disable rename folding so moving source into docs still includes the deleted source path.
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", comparison, "--"],
        cwd=cwd,
        capture_output=True,
        check=True,
    )
    return [
        path for path in result.stdout.decode("utf-8", errors="surrogateescape").split("\0") if path
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--files", nargs="*", help="Explicit paths for inspecting selection locally"
    )
    args = parser.parse_args()
    reason = "changed paths"
    if args.files is not None:
        selected = classify_changes(args.files)
    else:
        try:
            event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
            name = os.environ["GITHUB_EVENT_NAME"]
            if name == "workflow_dispatch":
                selected = set(SCOPES)
                reason = "manual full validation"
            else:
                pr = name == "pull_request"
                if name not in {"push", "pull_request"}:
                    raise ValueError("Unknown event")
                base = event["pull_request"]["base"]["sha"] if pr else event["before"]
                head = event["pull_request"]["head"]["sha"] if pr else event["after"]
                selected = classify_changes(changed_files(base, head, pull_request=pr))
        except (KeyError, ValueError, OSError, subprocess.CalledProcessError) as error:
            selected = set(SCOPES)
            reason = f"full validation: comparison unavailable ({type(error).__name__})"
    outputs = {scope: str(scope in selected).lower() for scope in SCOPES}
    print(json.dumps({**outputs, "reason": reason}))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as stream:
            for key, value in outputs.items():
                stream.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
