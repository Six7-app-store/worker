---
name: doku-nachziehen
description: Nach einer Code- oder Architekturänderung jede Doku und jedes Diagramm finden, das jetzt falsch ist, und nachziehen. Auch wenn das Commit-Gate „Doku nicht nachgezogen" meldet.
---

# Doku nachziehen

Doku veraltet nicht mit einem Knall, sondern leise: eine Tabelle nennt noch
den alten Ablauf, ein Diagramm zeigt einen Dienst, den es nicht mehr gibt.
Der Code ist richtig, die Beschreibung daneben falsch — und wer ihr folgt,
macht den Fehler.

Aufruf: `/doku-nachziehen`, oder wenn das Commit-Gate meldet:
`… geaendert, aber deployment/docs/… nicht`.

## 1. Was hat sich geändert

```bash
git diff --name-only origin/develop...HEAD
git status --porcelain
```

In jedem betroffenen Repo. Daraus zwei Listen: **Pfade** und **Begriffe**
(umbenannte Funktionen, Endpunkte, Env-Variablen, Tabellen, Dienste,
Make-Targets, Workflow-Eingaben).

## 2. Was beschreibt das

Zwei Quellen, beide nötig:

- **`deployment/harness/doc-map.json`** — welche Doku zu welchen Pfaden
  gehört. Das Commit-Gate liest dieselbe Datei.
- **Suche nach den Begriffen** — die Zuordnung kennt nur die großen
  Zusammenhänge:

  ```bash
  grep -rn "<begriff>" deployment/docs deployment/README.md */AGENTS.md
  ```

Fehlt in der Zuordnung ein Zusammenhang, der sich hier gezeigt hat: Regel in
`harness/doc-map.json` ergänzen (fragt nach, weil es Harness ist).

## 3. Nachziehen

- **Fließtext und Tabellen:** auf den neuen Stand. Nicht nur den Satz
  tauschen — prüfen, ob der Abschnitt danach noch stimmt.
- **Mermaid-Diagramme** (`docs/architektur.md` und andere): Knoten, Kanten
  und Beschriftungen an den neuen Stand. Stil des vorhandenen Diagramms
  beibehalten (Diagrammtyp, Richtung, Benennung). Ein Diagramm zeigt den
  Ist-Zustand; Geplantes gehört in den Plan, nicht ins Diagramm.
- **Stand-Zeile:** Dokumente mit `Stand: TT.MM.JJJJ` bekommen das heutige
  Datum.
- **AGENTS.md:** wenn sich ein Befehl, eine Falle oder die DoD geändert hat.
  Unter 60 Zeilen bleiben (siehe `docs/harness.md`).
- **ADR:** wird nie umgeschrieben. Hat sich eine Entscheidung geändert →
  neues ADR (`adr-schreiben`).

Die Doku liegt in `deployment`, der Code oft woanders: das sind zwei
Commits in zwei Repos. Die Doku-Änderung zuerst machen — das Commit-Gate im
Code-Repo sieht sie im Arbeitsbaum oder auf dem Branch von `deployment`.

## 4. Prüfen

```bash
cd deployment && make docs-check
```

Findet tote Links und Anker, fehlende ADR-Einträge, abweichende Status,
veraltete Zuordnungen und — mit Docker — Diagramme, die nicht rendern.
Findet **nicht**, ob ein Text inhaltlich stimmt. Das ist Schritt 3.

## Wenn wirklich nichts zu ändern ist

In die Commit-Nachricht:

```
Doku-geprueft: <konkreter Grund>
```

Ein Grund ist konkret, wenn ihn jemand ohne die Sitzung nachprüfen kann:
„reiner Refactor, Verhalten und Namen unverändert", „neues Feld nur intern,
im Datenmodell-Diagramm sind nur Tabellen". Kein Grund ist „n/a" oder
„nicht relevant" — die Zeile steht für immer in der Historie, und `/pr-review`
prüft, ob sie trägt.
