#!/usr/bin/env python3
"""Hook-Helfer für Claude Code im worker-Repo.

Ein Modus, liest das Hook-JSON von stdin:

    lint   PostToolUse auf Edit|Write — lässt ruff --fix und black im
           Container worker-dev über die gerade geänderte Datei laufen.
           Beides, weil die CI beides prüft.

Bewusst fail-open: jeder Fehler (kein Docker, kaputtes JSON, Container
aus) endet still mit Exit 0. Das harte Gate ist die CI.

Kein jq: das ist auf den Entwicklungsrechnern nicht überall installiert,
python dagegen zwangsläufig.
"""

import json
import subprocess
import sys

CONTAINER = "worker-dev"
# Alles unterhalb dieses Pfadsegments liegt im Container unter /app.
MOUNT_MARKER = "/worker/"


def hook_input() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def edited_path(data: dict) -> str:
    response = data.get("tool_response") or {}
    tool_input = data.get("tool_input") or {}
    return response.get("filePath") or tool_input.get("file_path") or ""


def lint(data: dict) -> None:
    # Windows-Backslashes zu Slashes, damit das Muster beide Seiten trifft.
    path = edited_path(data).replace("\\", "/")
    if not path.endswith(".py") or MOUNT_MARKER not in path:
        return
    # ../worker ist als /app gemountet, der Pfad muss übersetzt werden.
    in_container = "/app/" + path.rsplit(MOUNT_MARKER, 1)[1]
    for tool in (["ruff", "check", "--fix"], ["black", "-q"], ["isort", "-q"]):
        subprocess.run(
            ["docker", "exec", CONTAINER, "poetry", "run", *tool, in_container],
            capture_output=True,
            timeout=45,
        )


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if mode == "lint":
            lint(hook_input())
    except Exception:
        pass


if __name__ == "__main__":
    main()
