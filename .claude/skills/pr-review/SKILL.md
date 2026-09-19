---
name: pr-review
description: Einen Diff oder Pull Request gegen die Definition of Done des betroffenen Repos prüfen — mechanische Befunde belegt, Freigabe bleibt beim Menschen.
---

# PR-Review

Prüft eine Änderung, bevor ein Mensch sie liest. Das Ziel ist nicht die
Freigabe — die trifft immer eine Person. Das Ziel ist, dass der Mensch keine
Zeit mehr mit den mechanischen Fehlern verliert.

Aufruf: `/pr-review` (aktueller Diff) oder `/pr-review <PR-Nummer>`.

## Erst feststellen, worum es geht

```bash
git status --porcelain
git diff --stat
git diff              # oder: gh pr diff <nummer>
```

Aus den Pfaden ergibt sich das Repo — und damit die Regeln. **Die Definition
of Done steht in der `AGENTS.md` des betroffenen Repos.** Die ist die
Messlatte, nicht ein allgemeiner Review-Katalog. Bei einer Änderung über
mehrere Repos gilt jede Liste für ihren Teil.

## Was immer geprüft wird

| Prüfung | Woran es scheitert |
|---|---|
| Modelländerung ohne Migration | `app/models.py` geändert, nichts Neues in `alembic/versions/` |
| Bestehende Migration bearbeitet | Eine Datei in `alembic/versions/` steht als `M` statt `A` im Diff |
| Endpunkt ohne Frontend-Aufruf | Neue Route in `app/routers/`, aber `frontend/src/api/*.ts` unberührt |
| Auth am Helfer vorbei | `keycloak_auth` direkt importiert statt `get_current_user` |
| Berechtigung nur über Rolle | `require_*` da, aber kein `ensure_*` auf dem geladenen Objekt |
| View ruft API direkt | `*.vue` importiert `api/*.ts` statt über den Store zu gehen |
| Auth-Header von Hand | `Authorization` irgendwo außerhalb von `src/api/axios.ts` |
| Neue Env-Variable halb gepflegt | in einer Compose-Datei, aber nicht in `.env.example` — oder umgekehrt |
| Harness geändert ohne Sync | `deployment/harness/*` im Diff, die erzeugten `.claude/`-Kopien aber nicht — `make harness-sync` fehlt |
| Terraform unformatiert | `terraform fmt -check -recursive infrastructure/terraform` rot |
| Secret im Diff | Werte in `.env`-Form, `-----BEGIN`, Tokens, Passwörter im Klartext |
| Test fehlt | neuer Endpunkt/Task ohne Erfolgs- **und** Verweigerungsfall |
| ADR fehlt | Entscheidung getroffen, die teuer zurückzunehmen ist → `deployment/docs/adr/` |

## Was nicht ins Review gehört

- Formatierung, die `ruff`/`black`/`isort`/`prettier` ohnehin erledigen. Der
  Lint-Hook hat das schon angefasst; ein Befund darüber ist Rauschen.
- Geschmacksfragen ohne Regel in der `AGENTS.md`. Wer eine neue Regel für
  nötig hält, schlägt sie dort vor — nicht im Review eines fremden PRs.
- Umbauten, die über den Zweck der Änderung hinausgehen.

## Belegen, nicht behaupten

Ein Befund ohne Fundstelle ist nicht nachprüfbar und kostet den Menschen
genau die Zeit, die das Review sparen sollte.

```
backend/app/routers/apps.py:84
  Kein ensure_* nach dem Laden des Objekts. require_staff prüft die Rolle,
  nicht den Bezug zu diesem App-Eintrag. → ensure_edit_app(current_user, app, db=db)
```

Am Ende die Gates wirklich fahren, nicht vermuten:

```bash
docker exec backend-dev poetry run ruff check .
make test-backend-isolated
docker exec frontend-dev sh -lc 'cd /app && npx vue-tsc -b'
```

Läuft der Stack nicht, ist das die Antwort — nicht „Tests grün".

## Ergebnis

Drei Abschnitte, in dieser Reihenfolge:

1. **Blocker** — verletzt die Definition of Done. Mit Datei, Zeile, Regel.
2. **Anmerkungen** — wäre besser, hält den PR aber nicht auf.
3. **Geprüft und in Ordnung** — welche Gates gelaufen sind, mit Ausgabe.

Danach nichts weiter. **Kein „LGTM", kein Merge, kein `gh pr merge`.** Die
Freigabe ist eine menschliche Entscheidung; dieses Review ist die Vorarbeit
dafür. `gh pr merge` ist per deny-Regel gesperrt — das ist Absicht und kein
Fehler, der zu umgehen wäre.
