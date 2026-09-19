#!/usr/bin/env python3
"""Hook-Helfer für Claude Code — eine Datei für alle vier Repos.

Kanonische Fassung: ``deployment/harness/agent_guard.py``. Alle Kopien
unter ``<repo>/.claude/hooks/`` und ``DHBW_APP/.claude/hooks/`` erzeugt
``deployment/harness/sync.py``. Änderungen gehören hierher, nie in eine
Kopie — sonst driften die vier Repos wieder auseinander.

Drei Modi, alle lesen das Hook-JSON von stdin:

    pre    PreToolUse auf Edit|Write — blockt zwei Dinge:
           * Schreibzugriffe auf Dateien mit Geheimnissen (.env, *.pem,
             *.key). Gepflegt wird .env.example, nie .env.
           * Änderungen an bereits existierenden Alembic-Migrationen.
             Neue Migrationen bleiben erlaubt.
    lint   PostToolUse auf Edit|Write — formatiert die gerade geänderte
           Datei mit dem Werkzeug des Repos, zu dem sie gehört.
    stop   Stop — Qualitäts-Gate am Ende der Antwort.

Die Datei funktioniert aus jedem Arbeitsverzeichnis: sie leitet das
zuständige Repo aus dem Dateipfad ab, nicht aus dem cwd. Damit greift
derselbe Hook, egal ob die Sitzung in ``DHBW_APP/`` oder in einem der
vier Repos gestartet wurde.

Bewusst fail-open: jeder Fehler (kein Docker, kaputtes JSON, Container
aus) endet still mit Exit 0. Ein Hook, der die Sitzung blockiert, weil
er selbst defekt ist, kostet mehr als er schützt. Das harte Gate für
Lint und Tests ist die CI; die harte Grenze für Geheimnisse und
Produktion sind die deny-Regeln in ``.claude/settings.json``.

Kein jq: das ist auf den Entwicklungsrechnern nicht überall installiert,
python dagegen zwangsläufig.
"""

import contextlib
import json
import os
import subprocess
import sys

# ----------------------------------------------------------------
# Repo-Tabelle
# ----------------------------------------------------------------
# Jedes Repo: Container, Formatierer für eine einzelne Datei (lint) und
# Gate-Kommandos für das Ende der Antwort (stop).
REPOS = {
    "backend": {
        "container": "backend-dev",
        "format": [["poetry", "run", "ruff", "check", "--fix"]],
        "suffixes": (".py",),
        "gate": [
            (
                ["poetry", "run", "ruff", "check", "."],
                "ruff meldet Fehler. Ausfuehren und beheben: "
                "docker exec backend-dev poetry run ruff check .",
            )
        ],
    },
    "worker": {
        "container": "worker-dev",
        # Alle drei, weil die CI alle drei prüft.
        "format": [
            ["poetry", "run", "ruff", "check", "--fix"],
            ["poetry", "run", "black", "-q"],
            ["poetry", "run", "isort", "-q"],
        ],
        "suffixes": (".py",),
        "gate": [
            (
                ["poetry", "run", "ruff", "check", "."],
                "ruff meldet Fehler im worker. Beheben: "
                "docker exec worker-dev poetry run ruff check .",
            ),
            (
                ["poetry", "run", "black", "--check", "."],
                "black meldet ungeformten Code im worker. Beheben: "
                "docker exec worker-dev poetry run black .",
            ),
            (
                ["poetry", "run", "isort", "--check-only", "."],
                "isort meldet falsche Importreihenfolge im worker. Beheben: "
                "docker exec worker-dev poetry run isort .",
            ),
        ],
    },
    "frontend": {
        "container": "frontend-dev",
        # Bewusst kein Auto-Format: das Frontend hat keinen
        # PostToolUse-Formatierer, das Gate ist der Typecheck.
        "format": [],
        "suffixes": (),
        "gate": [
            (
                ["sh", "-lc", "cd /app && npx vue-tsc -b"],
                "Typecheck schlaegt fehl. Ausfuehren und beheben: "
                'docker exec frontend-dev sh -lc "cd /app && npx vue-tsc -b"',
            )
        ],
    },
    # deployment läuft nicht im Container: das Gate ist Terraform am Host.
    "deployment": {
        "container": None,
        "format": [],
        "suffixes": (),
        "gate": [],
    },
}

SECRET_REASON = (
    "Diese Datei traegt Geheimnisse und wird nicht vom Agenten geschrieben. "
    "Gepflegt wird .env.example (ohne Werte); die echte .env und alle "
    "*.pem-/*.key-Dateien setzt eine Person von Hand."
)

MIGRATION_REASON = (
    "Bestehende Alembic-Migrationen werden nie bearbeitet: eine geaenderte "
    "Migration zerlegt jede Datenbank, die sie bereits gefahren hat. "
    "Stattdessen eine neue erzeugen mit: docker exec backend-dev alembic "
    "revision --autogenerate -m '...'"
)


# ----------------------------------------------------------------
# Hook-Eingabe
# ----------------------------------------------------------------
def hook_input() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def edited_path(data: dict) -> str:
    """Den Pfad der geänderten Datei aus dem Hook-JSON ziehen.

    PostToolUse liefert ihn in ``tool_response.filePath``, PreToolUse nur
    in ``tool_input.file_path``.
    """
    response = data.get("tool_response") or {}
    tool_input = data.get("tool_input") or {}
    return response.get("filePath") or tool_input.get("file_path") or ""


def as_posix(path: str) -> str:
    """Windows-Backslashes zu Slashes, damit ein Muster beide Seiten trifft."""
    return path.replace("\\", "/")


def session_dirs(data: dict) -> tuple:
    """(Arbeitsverzeichnis, Ordner mit den vier Repos nebeneinander).

    Die Sitzung kann in einem Repo oder im Ordner darüber laufen; beide
    Fälle müssen dieselben Regeln ergeben.
    """
    cwd = data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    cwd = os.path.abspath(cwd)
    root = os.path.dirname(cwd) if os.path.basename(cwd) in REPOS else cwd
    return cwd, root


def resolve(path: str, cwd: str) -> str:
    """Pfad absolut machen.

    Edit und Write liefern ohnehin absolute Pfade. Ein relativer käme nur
    über einen anderen Aufrufweg herein und würde sonst keinem Repo
    zugeordnet — also still nicht formatiert.
    """
    if not path:
        return ""
    if os.path.isabs(path):
        return os.path.abspath(path)
    return os.path.abspath(os.path.join(cwd, path))


def repo_of(path: str, root: str) -> str:
    """Welches der vier Repos gehört zu diesem Pfad?

    Am **ersten** Segment unterhalb des Arbeitsordners entschieden, nicht am
    letzten passenden Namen irgendwo im Pfad: `frontend/src/backend/x.py`
    gehört zum frontend, nicht zum backend.
    """
    try:
        relative = os.path.relpath(path, root)
    except ValueError:
        # Anderes Laufwerk unter Windows — gehört zu keinem Repo.
        return ""
    first = as_posix(relative).split("/")[0]
    return first if first in REPOS else ""


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    # Bewusst ohne Umlaute: die Windows-Konsole laeuft nicht
                    # zwangslaeufig auf UTF-8, und diese Zeile wird gelesen,
                    # wenn gerade etwas schiefgeht.
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}))


def docker_available() -> bool:
    try:
        return (
            subprocess.run(
                ["docker", "ps"], capture_output=True, timeout=10
            ).returncode
            == 0
        )
    except Exception:
        return False


def container_running(name: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
            capture_output=True,
            timeout=10,
            text=True,
        )
        return name in (result.stdout or "")
    except Exception:
        return False


# ----------------------------------------------------------------
# pre — PreToolUse
# ----------------------------------------------------------------
def is_secret_file(path: str) -> bool:
    normalized = as_posix(path)
    name = normalized.rsplit("/", 1)[-1]
    if name.endswith(".pem") or name.endswith(".key"):
        return True
    if name in (".env.example", ".env.staging.example", ".secrets.template"):
        return False
    if name.endswith(".example") or name.endswith(".template"):
        return False
    return name == ".env" or name.startswith(".env.")


def is_existing_migration(path: str) -> bool:
    if "alembic/versions/" not in as_posix(path):
        return False
    # Eine neue Migration darf entstehen — tabu sind nur bestehende.
    return os.path.isfile(path)


def pre(data: dict) -> None:
    cwd, _ = session_dirs(data)
    path = resolve(edited_path(data), cwd)
    if not path:
        return
    if is_secret_file(path):
        deny(SECRET_REASON)
        return
    if is_existing_migration(path):
        deny(MIGRATION_REASON)


# ----------------------------------------------------------------
# lint — PostToolUse
# ----------------------------------------------------------------
def container_path(path: str, repo: str, root: str) -> str:
    """Hostpfad in den Containerpfad übersetzen (``<repo>/`` ist ``/app``)."""
    try:
        relative = as_posix(os.path.relpath(path, os.path.join(root, repo)))
    except ValueError:
        return ""
    if relative.startswith(".."):
        return ""
    return "/app/" + relative


def lint(data: dict) -> None:
    cwd, root = session_dirs(data)
    path = resolve(edited_path(data), cwd)
    repo = repo_of(path, root)
    if not repo:
        return

    # Terraform formatiert der Host, nicht ein Container.
    if as_posix(path).endswith((".tf", ".tfvars")):
        with contextlib.suppress(Exception):
            subprocess.run(["terraform", "fmt", path], capture_output=True, timeout=30)
        return

    config = REPOS[repo]
    suffixes = config["suffixes"]
    if not suffixes or not as_posix(path).endswith(suffixes):
        return
    in_container = container_path(path, repo, root)
    if not in_container or not config["container"]:
        return
    if not container_running(config["container"]):
        return
    for tool in config["format"]:
        try:
            subprocess.run(
                ["docker", "exec", config["container"], *tool, in_container],
                capture_output=True,
                timeout=45,
            )
        except Exception:
            return


# ----------------------------------------------------------------
# stop — Stop
# ----------------------------------------------------------------
def git_dirty(repo_dir: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", repo_dir, "status", "--porcelain"],
            capture_output=True,
            timeout=20,
            text=True,
        )
        return bool((result.stdout or "").strip())
    except Exception:
        return False


def gate_repo(repo: str, repo_dir: str) -> list:
    """Die Gate-Kommandos eines Repos fahren, Meldungen sammeln."""
    reasons = []

    if repo == "deployment":
        terraform_dir = os.path.join(repo_dir, "infrastructure", "terraform")
        if os.path.isdir(terraform_dir):
            try:
                result = subprocess.run(
                    ["terraform", "fmt", "-check", "-recursive", terraform_dir],
                    capture_output=True,
                    timeout=60,
                )
                if result.returncode != 0:
                    reasons.append(
                        "Terraform ist nicht formatiert. Die Pipeline bricht darauf "
                        "ab. Beheben: terraform fmt -recursive "
                        "infrastructure/terraform"
                    )
            except Exception:
                pass
        return reasons

    config = REPOS[repo]
    container = config["container"]
    if not container or not container_running(container):
        return reasons
    for command, reason in config["gate"]:
        try:
            result = subprocess.run(
                ["docker", "exec", container, *command],
                capture_output=True,
                timeout=240,
            )
        except Exception:
            return reasons
        if result.returncode != 0:
            reasons.append(reason)
    return reasons


def stop(data: dict) -> None:
    # Schutz vor der Endlosschleife: laeuft die Antwort bereits wegen
    # dieses Hooks weiter, wird nicht erneut geblockt. Sonst haengt eine
    # Sitzung an einem Fehler fest, den Claude nicht beheben kann.
    if data.get("stop_hook_active"):
        return

    cwd, root = session_dirs(data)
    here = os.path.basename(cwd)

    if here in REPOS:
        # Sitzung laeuft im Repo: dessen Gate immer fahren.
        targets = [(here, cwd)]
    else:
        # Sitzung laeuft im Arbeitsordner ueber allen vier Repos. Nur die
        # Repos pruefen, in denen wirklich etwas geaendert wurde — sonst
        # kostet jeder Stop einen vollen Typecheck ueber alles.
        targets = []
        for repo in REPOS:
            repo_dir = os.path.join(root, repo)
            if os.path.isdir(repo_dir) and git_dirty(repo_dir):
                targets.append((repo, repo_dir))

    if not targets:
        return
    if any(REPOS[repo]["container"] for repo, _ in targets) and not docker_available():
        return

    reasons = []
    for repo, repo_dir in targets:
        reasons.extend(f"[{repo}] {reason}" for reason in gate_repo(repo, repo_dir))
    if reasons:
        block("\n".join(reasons))


# ----------------------------------------------------------------
def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        data = hook_input()
        if mode == "pre":
            pre(data)
        elif mode == "lint":
            lint(data)
        elif mode == "stop":
            stop(data)
    except Exception:
        pass


if __name__ == "__main__":
    main()
