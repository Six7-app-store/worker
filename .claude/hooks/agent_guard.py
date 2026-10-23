#!/usr/bin/env python3
"""Hook-Helfer für Claude Code — eine Datei für alle vier Repos.

Kanonische Fassung: ``deployment/harness/agent_guard.py``. Alle Kopien
unter ``<repo>/.claude/hooks/`` und ``DHBW_APP/.claude/hooks/`` erzeugt
``deployment/harness/sync.py``. Änderungen gehören hierher, nie in eine
Kopie — sonst driften die vier Repos wieder auseinander.

Vier Modi, alle lesen das Hook-JSON von stdin:

    pre    PreToolUse auf Edit|Write — blockt zwei Dinge:
           * Schreibzugriffe auf Dateien mit Geheimnissen (.env, *.pem,
             *.key). Gepflegt wird .env.example, nie .env.
           * Änderungen an bereits existierenden Alembic-Migrationen.
             Neue Migrationen bleiben erlaubt.
    bash   PreToolUse auf Bash — vier Prüfungen am zerlegten Kommando:
           * Geheimnisse: blockt Kommandos, die eine Datei mit
             Geheimnissen lesen würden. Die deny-Regeln greifen nur am
             Read-Werkzeug; eine Shell geht daran vorbei. Entschieden wird
             am Argument, nie am Kommandostring als Ganzem: eine Suche
             darin kann ein Kommando nicht von seiner Erwähnung in einer
             Commit-Nachricht unterscheiden. Aufrufe, die Geheimnisse ohne
             Dateinamen ausgeben, decken die deny-Regeln ab — die wertet
             Claude Code selbst aus und kennt Trennzeichen und Quoting.
           * Ziel-Repo: schreibende gh-Aufrufe nur mit -R Six7-app-store/…
             Die Stack-Repos sind Forks; ohne -R zielt gh auf das Original.
           * Staging: `gh workflow run staging.yml` nur für den Stand
             develop und nur, wenn kein Deploy läuft (ADR 0015).
           * Commit-Gate: vor `git commit` die Doku-Zuordnung aus
             harness/doc-map.json und die Tests der CI (ADR 0017).
    lint   PostToolUse auf Edit|Write — formatiert die gerade geänderte
           Datei mit dem Werkzeug des Repos, zu dem sie gehört.
    stop   Stop — Qualitäts-Gate am Ende der Antwort.

Die Datei funktioniert aus jedem Arbeitsverzeichnis: sie leitet das
zuständige Repo aus dem Dateipfad ab, nicht aus dem cwd. Damit greift
derselbe Hook, egal ob die Sitzung in ``DHBW_APP/`` oder in einem der
vier Repos gestartet wurde.

Bewusst fail-open, mit zwei Ausnahmen: jeder Fehler (kein Docker,
kaputtes JSON, Container aus) endet still mit Exit 0. Ein Hook, der die
Sitzung blockiert, weil er selbst defekt ist, kostet mehr als er schützt.
Die harte Grenze für Geheimnisse und Produktion sind die deny-Regeln in
``.claude/settings.json``.

Die Ausnahmen sind der Staging-Deploy und das Commit-Gate. Beide
schließen, wenn sie nicht prüfen können: ein Deploy reißt eine VM ab, und
ein Commit ohne gelaufene Tests ist genau der Fall, den das Gate
verhindern soll (ADR 0015, ADR 0017). Ein Mensch committet am eigenen
Terminal weiterhin ohne Hook.

Kein jq: das ist auf den Entwicklungsrechnern nicht überall installiert,
python dagegen zwangsläufig.
"""

import contextlib
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

# ----------------------------------------------------------------
# Repo-Tabelle
# ----------------------------------------------------------------
# Jedes Repo: Container, Formatierer für eine einzelne Datei (lint),
# Gate-Kommandos für das Ende der Antwort (stop) und die Tests, die das
# Commit-Gate zusätzlich fährt (tests). Die Tests sind genau die der CI,
# nur ohne Coverage-Schwelle — die bleibt Sache der Pipeline.
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
        # Ohne TEST_DATABASE_URL leert die Suite die Dev-Datenbank. Die
        # Compose-Datei setzt sie zwar, aber ein Gate verlaesst sich nicht
        # darauf, dass niemand die Compose-Datei aendert.
        "tests": [
            "-e",
            "TEST_DATABASE_URL=postgresql://postgres:postgres@postgres-test:5432/backend_test",
            "backend-dev",
            "poetry", "run", "pytest", "-x", "-q", "--no-cov",
        ],
        "needs": ("postgres-test-dev",),
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
        # mypy laeuft in der CI mit continue-on-error und gehoert deshalb
        # nicht hierher: ein lokales Gate, das strenger ist als die CI,
        # erzeugt Arbeit, die niemand verlangt.
        "tests": ["worker-dev", "poetry", "run", "pytest", "-x", "-q", "--no-cov"],
        "needs": (),
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
        # eslint fehlt mit Absicht: die CI faehrt es nicht.
        "tests": ["frontend-dev", "sh", "-lc", "cd /app && npx vitest --run"],
        "needs": (),
    },
    # deployment läuft nicht im Container: das Gate ist OpenTofu am Host
    # (sonst über Docker), dazu der Doku-Check.
    "deployment": {
        "container": None,
        "format": [],
        "suffixes": (),
        "gate": [],
        "tests": [],
        "needs": (),
    },
}

# Das einzige Ziel fuer schreibende gh-Aufrufe. Die Stack-Repos sind
# GitHub-Forks von DHBW-AppStore; ohne -R waehlt gh das Original.
GH_OWNER = "Six7-app-store"

# Fallback, wenn tofu nicht am Host liegt: dieselbe Version wie in der CI
# (infra-qa.yml, staging.yml).
IAC_IMAGES = {
    "tofu": "ghcr.io/opentofu/opentofu:1.13.2",
}

# Wer bewusst committet, obwohl die zugeordnete Doku unberuehrt ist,
# schreibt diese Zeile mit Begruendung in die Commit-Nachricht. Die
# Begruendung steht damit in der Historie, nicht in einem Chat.
DOC_ACK = "Doku-geprueft:"

# Ausgaben von git, gh, Docker und den Testlaeufen kommen als UTF-8. Mit
# **TEXT dekodiert Python unter Windows als cp1252 und scheitert an
# einzelnen Bytes — im Lesethread, also still: stdout ist dann None. Ein
# Commit-Titel mit Umlaut haette so den Staging-Waechter blockiert.
TEXT = {"encoding": "utf-8", "errors": "replace"}

SECRET_REASON = (
    "Diese Datei traegt Geheimnisse und wird nicht vom Agenten geschrieben. "
    "Gepflegt wird .env.example (ohne Werte); die echte .env und alle "
    "*.pem-/*.key-Dateien setzt eine Person von Hand."
)

SECRET_COMMAND_REASON = (
    "Dieses Kommando wuerde eine Datei mit Geheimnissen lesen. Die deny-Regeln "
    "sperren das Read-Werkzeug, nicht die Shell — deshalb faengt es dieser Hook "
    "ab. Gepflegt und frei lesbar ist .env.example."
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
            **TEXT,
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
# bash — PreToolUse auf Bash
# ----------------------------------------------------------------
# Pfadartige Zeichenketten mit verdaechtiger Endung — auch mitten in einem
# Argument, etwa in einem python-Einzeiler mit open(...).
# Bis zum Ende des Dateinamens, sonst beurteilt is_secret_file ein
# Fragment: aus einer Vorlage mit mehreren Endungen wuerde sonst ein
# Geheimnis.
SECRET_PATH = re.compile(r"[\w./\\-]*\.(?:env|pem|key)[\w.-]*")


def secret_token(command: str) -> str:
    """Die erste Stelle im Kommando, die auf eine Geheimnisdatei zeigt.

    Absichtlich am Dateinamen entschieden und nicht am Programm: `cat` zu
    sperren hilft nichts, solange `head`, `sed`, `awk`, `base64` und ein
    Dreizeiler in python dasselbe können. Ein Muster über die Leseprogramme
    ist nicht zu gewinnen, eines über den Dateinamen schon.

    Zwei Durchgänge, weil ein Pfad auf zwei Arten auftaucht: als eigenes
    Argument und eingebettet in eine Zeichenkette innerhalb des Kommandos.

    Zwei bewusste Kosten. Erstens fängt das auch Kommandos, die gar nichts
    lesen — wer den Dateinamen nur erwähnt, wird ebenfalls geblockt. Das ist
    die Richtung, in die ein Fehler bei Geheimnissen fallen soll. Zweitens
    bleibt ein Pfad unentdeckt, der erst zur Laufzeit entsteht. Dagegen hilft
    nur, dass Bash ohnehin nachfragt, solange ein Kommando nicht in der
    allow-Liste steht.
    """
    for raw in command.replace("=", " ").split():
        token = raw.strip("\"'`()<>;|&")
        if token and is_secret_file(token):
            return token
    for treffer in SECRET_PATH.findall(command):
        if is_secret_file(treffer):
            return treffer
    return ""


def segments(command: str) -> list:
    """Das Kommando als Liste einfacher Kommandos, je eine Wortliste.

    Getrennt wird an ``&&``, ``||``, ``;``, ``|``, Klammern und
    Zeilenumbruch, aber nur ausserhalb von Anfuehrungszeichen — eine
    Commit-Nachricht, in der "git commit" steht, ist kein Commit.

    Bricht das Quoting (ein einzelnes Anfuehrungszeichen in einem Heredoc
    ohne umschliessende Quotes), zaehlt nur die erste Zeile: dort steht das
    Kommando selbst, darunter der Text.
    """
    def zerlegen(text: str) -> list:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()\n")
        lexer.whitespace_split = True
        lexer.whitespace = " \t\r"
        return list(lexer)

    try:
        tokens = zerlegen(command)
    except ValueError:
        try:
            tokens = zerlegen(command.splitlines()[0] if command else "")
        except ValueError:
            return []
    result, current = [], []
    for token in tokens:
        if token and not token.strip(";&|()\n"):
            if current:
                result.append(current)
            current = []
        else:
            current.append(token)
    if current:
        result.append(current)
    return result


def program(words: list) -> tuple:
    """(Programmname, Argumente) nach fuehrenden VAR=wert-Zuweisungen."""
    index = 0
    while index < len(words) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[index]):
        index += 1
    if index >= len(words):
        return "", []
    name = as_posix(words[index]).rsplit("/", 1)[-1]
    if name.endswith(".exe"):
        name = name[:-4]
    return name, words[index + 1:]


def option_values(args: list, *names: str) -> list:
    """Alle Werte einer Option, ob als ``-x wert``, ``--xx wert`` oder ``--xx=wert``."""
    values = []
    for index, arg in enumerate(args):
        for name in names:
            if arg == name and index + 1 < len(args):
                values.append(args[index + 1])
            elif name.startswith("--") and arg.startswith(name + "="):
                values.append(arg[len(name) + 1:])
    return values


def git_call(args: list) -> tuple:
    """(Unterkommando, -C-Verzeichnis oder "", Rest) eines git-Aufrufs."""
    index, directory = 0, ""
    while index < len(args) and args[index].startswith("-"):
        if args[index] == "-C" and index + 1 < len(args):
            directory = args[index + 1]
            index += 2
        elif args[index] in ("-c", "--git-dir", "--work-tree", "--namespace"):
            index += 2
        else:
            index += 1
    if index >= len(args):
        return "", directory, []
    return args[index], directory, args[index + 1:]


# Schreibende gh-Aufrufe, die ein Ziel-Repo brauchen. Lesende fehlen mit
# Absicht: ein falsches Ziel beim Lesen fuehrt zu einer falschen Antwort,
# nicht zu einem Issue oder PR im Repository der Stakeholder.
GH_WRITES = {
    "pr": {"create", "edit", "comment", "close", "reopen", "ready", "review"},
    "issue": {"create", "edit", "comment", "close", "reopen", "delete", "transfer", "pin", "lock"},
    "label": {"create", "edit", "delete", "clone"},
    "workflow": {"run", "enable", "disable"},
    "run": {"rerun", "cancel", "delete"},
    "release": {"create", "edit", "delete", "upload"},
    "repo": {"edit"},
}

GH_REPO_REASON = (
    "gh {aufruf} ohne -R {owner}/<repo>. backend, deployment, frontend, "
    "worker, Ubuntu-App und template-app sind Forks von DHBW-AppStore; ohne "
    "-R zielt gh auf das Original. Ziel ist ausschliesslich {owner}."
)


def gh_target_reason(name: str, args: list) -> str:
    if name != "gh" or len(args) < 2:
        return ""
    if args[1] not in GH_WRITES.get(args[0], ()):
        return ""
    aufruf = f"{args[0]} {args[1]}"
    repos = option_values(args, "-R", "--repo")
    if args[0] == "repo":
        # `gh repo edit` nimmt das Repo als Positionsargument, nicht als -R.
        repos = [arg for arg in args[2:] if not arg.startswith("-")][:1]
    if not repos:
        return GH_REPO_REASON.format(aufruf=aufruf, owner=GH_OWNER)
    for repo in repos:
        owner = as_posix(repo).replace("https://", "").replace("github.com/", "").split("/")[0]
        if owner.lower() != GH_OWNER.lower():
            return (
                f"gh {aufruf} -R {repo}: Ziel ist ausschliesslich {GH_OWNER}. "
                "DHBW-AppStore ist nur Quelle zum Abgleichen, nie Ziel."
            )
    return ""


STAGING_WORKFLOWS = ("staging.yml", ".github/workflows/staging.yml", "CD - Staging Deployment")

STAGING_REASON = (
    "Der Agent baut nur den Stand develop neu (ADR 0015). Erwartet: gh "
    "workflow run staging.yml -R {owner}/deployment --ref develop -f "
    "stand=develop [-f recreate=false] [-f seed=true]. Der Stand main ist "
    "Produktion (ADR 0013) und bleibt bei einer Person."
)

# Status eines Laufs, der noch nicht fertig ist.
RUN_ACTIVE = {"queued", "in_progress", "waiting", "requested", "pending"}


def active_staging_runs() -> list:
    """Laufende oder wartende Staging-Deploys, None wenn nicht ermittelbar."""
    try:
        result = subprocess.run(
            [
                "gh", "run", "list", "-R", f"{GH_OWNER}/deployment",
                "--workflow", "staging.yml", "--limit", "20",
                "--json", "databaseId,status,displayTitle,createdAt",
            ],
            capture_output=True,
            timeout=30,
            **TEXT,
        )
        if result.returncode != 0:
            return None
        return [run for run in json.loads(result.stdout or "[]") if run.get("status") in RUN_ACTIVE]
    except Exception:
        return None


def staging_dispatch_reason(name: str, args: list) -> str:
    if name != "gh" or args[:2] != ["workflow", "run"] or len(args) < 3:
        return ""
    if args[2] not in STAGING_WORKFLOWS:
        return ""
    regel = STAGING_REASON.format(owner=GH_OWNER)
    if "--json" in args:
        return "Eingaben per --json sind hier nicht pruefbar. " + regel
    felder = {}
    for feld in option_values(args, "-f", "-F", "--field", "--raw-field"):
        key, _, value = feld.partition("=")
        felder[key] = value
    if felder.get("stand") != "develop":
        return regel
    if option_values(args, "--ref", "-r") != ["develop"]:
        return regel
    laeufe = active_staging_runs()
    if laeufe is None:
        return (
            "Ob gerade ein Staging-Deploy laeuft, liess sich nicht feststellen "
            "(gh run list scheiterte). Ein zweiter Lauf reisst die VM unter dem "
            "ersten weg; ohne diese Auskunft wird nicht ausgeloest."
        )
    if laeufe:
        offen = ", ".join(f"#{run.get('databaseId')} ({run.get('status')})" for run in laeufe)
        return (
            f"Es laeuft bereits ein Staging-Deploy: {offen}. Abwarten mit "
            f"gh run watch <id> -R {GH_OWNER}/deployment, dann neu entscheiden."
        )
    return ""


def bash(data: dict) -> None:
    command = (data.get("tool_input") or {}).get("command") or ""
    if not command:
        return
    token = secret_token(command)
    if token:
        deny(f"{SECRET_COMMAND_REASON} Ausloeser: {token}")
        return

    cwd, _ = session_dirs(data)
    here = cwd
    for words in segments(command):
        name, args = program(words)
        if name == "cd" and args:
            here = resolve(os.path.expanduser(args[0]), here)
            continue
        reason = gh_target_reason(name, args) or staging_dispatch_reason(name, args)
        if reason:
            deny(reason)
            return
        if name != "git":
            continue
        sub, directory, rest = git_call(args)
        if sub != "commit" or "--dry-run" in rest:
            continue
        commit_dir = resolve(directory, here) if directory else here
        try:
            reason = commit_gate(commit_dir, rest, command)
        except Exception as error:
            # Schliessen statt schweigen: ein defektes Gate darf nicht
            # aussehen wie ein gruenes.
            reason = (
                f"Das Commit-Gate ist selbst gescheitert ({type(error).__name__}: "
                f"{error}). Nicht umgehen: harness/agent_guard.py reparieren oder "
                "eine Person committet am eigenen Terminal."
            )
        if reason:
            deny(reason)
            return


# ----------------------------------------------------------------
# Commit-Gate (Teil von bash)
# ----------------------------------------------------------------
def git_out(repo_dir: str, *args: str) -> str:
    """Ausgabe eines git-Aufrufs, "" bei Fehler."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_dir, *args],
            capture_output=True,
            timeout=20,
            **TEXT,
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def changed_files(repo_dir: str) -> list:
    """Was der Commit enthalten kann: Index, Ungestagtes und Ungetracktes.

    Bewusst die Vereinigung, nicht nur der Index: ``git commit -a`` und
    ``git commit <pfad>`` nehmen auch Ungestagtes mit. Und der Hook laeuft
    vor dem ganzen Kommando — bei ``git add -A && git commit`` ist eine neue
    Datei in diesem Moment noch ungetrackt. Ohne sie ginge ein Commit, der
    nur aus neuen Dateien besteht, ohne Tests durch. Im Zweifel laufen die
    Tests einmal zu oft, nie einmal zu wenig.
    """
    names = set()
    for args in (
        ("diff", "--cached", "--name-only"),
        ("diff", "--name-only"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        names.update(line.strip() for line in git_out(repo_dir, *args).splitlines() if line.strip())
    return sorted(names)


def touched_since_base(repo_dir: str) -> set:
    """Pfade, die auf dem aktuellen Branch oder im Arbeitsbaum geaendert sind."""
    names = set()
    for line in git_out(repo_dir, "status", "--porcelain").splitlines():
        if len(line) > 3:
            names.add(line[3:].strip().split(" -> ")[-1].strip('"'))
    for base in ("origin/develop", "origin/main"):
        if git_out(repo_dir, "rev-parse", "--verify", "-q", base):
            diff = git_out(repo_dir, "diff", "--name-only", f"{base}...HEAD")
            names.update(line.strip() for line in diff.splitlines() if line.strip())
            break
    return names


def load_doc_map(root: str) -> list:
    path = os.path.join(root, "deployment", "harness", "doc-map.json")
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle).get("regeln", [])
    except Exception:
        return []


def matches(path: str, patterns: list) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def doc_reasons(repo: str, root: str, files: list, command: str) -> list:
    """Zugeordnete Doku, die zu dieser Aenderung unberuehrt geblieben ist."""
    if DOC_ACK in command:
        return []
    rules = [rule for rule in load_doc_map(root) if rule.get("repo") == repo]
    if not rules:
        return []
    touched = None
    reasons = []
    for rule in rules:
        hits = [path for path in files if matches(path, rule.get("pfade", []))]
        if not hits:
            continue
        if touched is None:
            touched = touched_since_base(os.path.join(root, "deployment"))
            if repo == "deployment":
                touched |= set(files)
        docs = rule.get("doku", [])
        if any(doc in touched for doc in docs):
            continue
        reasons.append(
            f"{', '.join(hits[:3])}{' …' if len(hits) > 3 else ''} geaendert, aber "
            f"{' / '.join('deployment/' + doc for doc in docs)} nicht ({rule.get('warum', '')})."
        )
    if reasons:
        reasons.append(
            "Doku und Diagramme nachziehen (Skill doku-nachziehen), oder bewusst "
            f"ohne Doku committen mit der Zeile '{DOC_ACK} <Grund>' in der "
            "Commit-Nachricht."
        )
    return reasons


def needs_tests(files: list) -> bool:
    """Reine Doku- und Harness-Kopien brauchen keinen Testlauf."""
    for path in files:
        if path.endswith(".md") or path.startswith(".claude/"):
            continue
        return True
    return False


def normalized_dir(path: str) -> str:
    """Hostpfad und Docker-Mountquelle auf eine vergleichbare Form bringen.

    Docker Desktop meldet je nach Version ``C:\\Users\\…``,
    ``/run/desktop/mnt/host/c/Users/…`` oder ``/c/Users/…``; git meldet
    ``C:/Users/…``.
    """
    posix = as_posix(path).rstrip("/").lower()
    for prefix in ("/run/desktop/mnt/host/", "/host_mnt/", "/mnt/"):
        if posix.startswith(prefix):
            posix = "/" + posix[len(prefix):]
    drive = re.match(r"^/([a-z])/(.*)$", posix)
    if drive:
        posix = f"{drive.group(1)}:/{drive.group(2)}"
    return posix


def mount_source(container: str) -> str:
    try:
        result = subprocess.run(
            [
                "docker", "inspect", container, "--format",
                '{{range .Mounts}}{{if eq .Destination "/app"}}{{.Source}}{{end}}{{end}}',
            ],
            capture_output=True,
            timeout=10,
            **TEXT,
        )
        return (result.stdout or "").strip()
    except Exception:
        return ""


def tail(text: str, lines: int = 25) -> str:
    rest = (text or "").strip().splitlines()
    return "\n".join(rest[-lines:])


def iac_fmt_reasons(repo_dir: str, strict: bool) -> list:
    """fmt -check auf beiden IaC-Baeumen; am Host, sonst ueber Docker.

    ``strict`` entscheidet, was passiert, wenn weder Werkzeug noch Docker da
    sind: das Stop-Gate schweigt dann, das Commit-Gate meldet es.
    """
    reasons = []
    for tree, tool in IAC_TREES:
        tree_dir = os.path.join(repo_dir, "infrastructure", tree)
        if not os.path.isdir(tree_dir):
            continue
        if shutil.which(tool):
            command = [tool, "fmt", "-check", "-recursive", tree_dir]
        elif docker_available():
            command = [
                "docker", "run", "--rm", "-v", f"{tree_dir}:/w", "-w", "/w",
                IAC_IMAGES[tool], "fmt", "-check", "-recursive", ".",
            ]
        else:
            if strict:
                reasons.append(
                    f"infrastructure/{tree} ist nicht pruefbar: weder {tool} noch "
                    "Docker verfuegbar. Docker Desktop starten."
                )
            continue
        try:
            result = subprocess.run(command, capture_output=True, timeout=120, **TEXT)
        except Exception:
            if strict:
                reasons.append(f"{tool} fmt -check auf infrastructure/{tree} lief nicht durch.")
            continue
        if result.returncode != 0:
            reasons.append(
                f"infrastructure/{tree} ist nicht formatiert. Die Pipeline "
                f"bricht darauf ab. Beheben: {tool} fmt -recursive "
                f"infrastructure/{tree}"
            )
    return reasons


def docs_check_reasons(repo_dir: str) -> list:
    """Der schnelle Teil von ``make docs-check``: Links, ADR-Verzeichnis, Zuordnung."""
    script = os.path.join(repo_dir, "scripts", "docs_check.py")
    if not os.path.isfile(script):
        return []
    try:
        result = subprocess.run(
            [sys.executable, script, "--mermaid=off"],
            capture_output=True,
            timeout=60,
            **TEXT,
            cwd=repo_dir,
        )
    except Exception:
        return []
    if result.returncode == 0:
        return []
    return [f"Doku-Check rot (make docs-check):\n{tail(result.stdout + result.stderr)}"]


def deployment_commit_reasons(repo_dir: str, files: list) -> list:
    reasons = docs_check_reasons(repo_dir)
    if any(path.startswith("infrastructure/") for path in files):
        reasons += iac_fmt_reasons(repo_dir, strict=True)
    if any(path.startswith("harness/") for path in files):
        try:
            result = subprocess.run(
                [sys.executable, os.path.join(repo_dir, "harness", "test_agent_guard.py")],
                capture_output=True,
                timeout=120,
                **TEXT,
                cwd=repo_dir,
            )
            if result.returncode != 0:
                reasons.append(f"make harness-test ist rot:\n{tail(result.stderr)}")
        except Exception:
            reasons.append("make harness-test lief nicht durch.")
    return reasons


def container_commit_reasons(repo: str, repo_dir: str) -> list:
    config = REPOS[repo]
    container = config["container"]
    hinweis = "Stack starten (cd deployment && make dev-up) und erneut committen."
    if not docker_available():
        return [f"Docker laeuft nicht; die Tests von {repo} sind nicht gelaufen. {hinweis}"]
    for needed in (container, *config["needs"]):
        if not container_running(needed):
            return [f"Container {needed} laeuft nicht; die Tests von {repo} sind nicht gelaufen. {hinweis}"]

    # Der Container testet, was unter /app gemountet ist. Liegt der Commit
    # in einem anderen Checkout (Worktree, zweite Kopie), waere ein gruener
    # Lauf ein Beleg fuer fremden Code.
    source = mount_source(container)
    if source and normalized_dir(source) != normalized_dir(repo_dir):
        return [
            f"{container} testet {source}, der Commit liegt in {repo_dir}. Ein "
            "gruener Lauf dort belegt diesen Stand nicht. Im gemounteten Checkout "
            "committen oder den Stack aus diesem Arbeitsordner starten."
        ]

    reasons = []
    for command, reason in config["gate"]:
        try:
            result = subprocess.run(
                ["docker", "exec", container, *command],
                capture_output=True,
                timeout=240,
                **TEXT,
            )
        except Exception:
            return [f"{reason} (Zeitlimit)"]
        if result.returncode != 0:
            reasons.append(f"{reason}\n{tail(result.stdout + result.stderr, 15)}")
    if reasons:
        return reasons

    try:
        result = subprocess.run(
            ["docker", "exec", *config["tests"]],
            capture_output=True,
            # Backend gemessen am 23.10.2026: 351 s. Luft bis zum Hook-Limit
            # von 900 s in sync.py bleibt fuer ruff davor.
            timeout=780,
            **TEXT,
        )
    except Exception:
        return [f"Die Tests von {repo} liefen ins Zeitlimit (13 Minuten)."]
    if result.returncode != 0:
        return [
            f"Tests von {repo} rot. Erst beheben, dann committen:\n"
            f"{tail(result.stdout + result.stderr)}"
        ]
    return []


def commit_gate(commit_dir: str, args: list, command: str) -> str:
    """Grund gegen den Commit, oder "" wenn er durchgehen darf."""
    top = git_out(commit_dir, "rev-parse", "--show-toplevel").strip()
    if not top:
        return ""
    top = os.path.abspath(top)
    repo = os.path.basename(top)
    if repo not in REPOS:
        return ""
    files = changed_files(top)
    if not files:
        return ""

    root = os.path.dirname(top)
    reasons = doc_reasons(repo, root, files, command)
    # Fehlende Doku zuerst melden: sie aendert den Commit ohnehin noch, ein
    # Testlauf davor waere verschenkte Zeit.
    if not reasons:
        if repo == "deployment":
            reasons = deployment_commit_reasons(top, files)
        elif needs_tests(files):
            reasons = container_commit_reasons(repo, top)
    if not reasons:
        return ""
    return f"Commit-Gate [{repo}] (ADR 0017):\n" + "\n".join(reasons)


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


# Welcher IaC-Baum mit welchem Werkzeug gepflegt wird. Seit ADR 0010 und
# ADR 0019 gibt es nur noch infrastructure/tofu.
IAC_TREES = (("tofu", "tofu"),)


def iac_formatter(path: str):
    """Das fmt-Kommando für eine IaC-Datei, sonst None.

    Die Endung entscheidet: OpenTofu liest .tofu-Dateien. Eine .tf-Datei gibt es
    in dieser Plattform nicht mehr (ADR 0019).
    """
    posix = as_posix(path)
    if posix.endswith(".tofu"):
        return ["tofu", "fmt", path]
    return None


def lint(data: dict) -> None:
    cwd, root = session_dirs(data)
    path = resolve(edited_path(data), cwd)
    repo = repo_of(path, root)
    if not repo:
        return

    # IaC formatiert der Host, nicht ein Container.
    formatter = iac_formatter(path)
    if formatter:
        with contextlib.suppress(Exception):
            subprocess.run(formatter, capture_output=True, timeout=30)
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
            **TEXT,
        )
        return bool((result.stdout or "").strip())
    except Exception:
        return False


def gate_repo(repo: str, repo_dir: str) -> list:
    """Die Gate-Kommandos eines Repos fahren, Meldungen sammeln."""
    reasons = []

    if repo == "deployment":
        # Vorher lief hier nur tofu/terraform am Host. Lag keins davon im
        # PATH, schwieg das Gate, ohne dass es jemand merkte.
        return iac_fmt_reasons(repo_dir, strict=False) + docs_check_reasons(repo_dir)

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
        elif mode == "bash":
            bash(data)
        elif mode == "lint":
            lint(data)
        elif mode == "stop":
            stop(data)
    except Exception:
        pass


if __name__ == "__main__":
    main()
