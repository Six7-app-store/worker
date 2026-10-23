---
name: issue-planen
description: Aus einem Story-Issue einen geprüften Umsetzungsplan machen — Ist-Stand gegen den aktuellen Code belegen, Abnahme festlegen, Doku und ADR einplanen, Plan als Kommentar ins Issue. Danach die Kette bis Staging.
---

# Issue planen

Macht aus einem Issue eine Spezifikation, nach der ein Agent ohne Rückfragen
bauen kann — und an der ein Mensch in zwei Minuten sieht, ob das Richtige
gebaut wird.

Aufruf: `/issue-planen <nr>` (Issue in `Six7-app-store/deployment`)

## 1. Lesen

```bash
gh issue view <nr> -R Six7-app-store/deployment --comments
```

Dann das verlinkte Arbeitspaket im Umsetzungsplan, die verlinkten ADRs und
die `AGENTS.md` jedes betroffenen Repos. Die DoD dort ist die Messlatte.

## 2. Ist-Stand belegen — nicht aus dem Issue übernehmen

Der Befund im Issue stammt aus einer Momentaufnahme. Seitdem kann sich
alles geändert haben (P15 etwa ist mit ADR 0010 erledigt worden). Jede
Aussage über den Ist-Stand braucht eine Fundstelle von **heute**:
`backend/app/routers/apps.py:84`, nicht „das Backend kann X nicht".

Ist die Story inzwischen erfüllt: Beleg als Kommentar, Schließen
vorschlagen, nicht planen.

## 3. Fachliches klären, nicht raten

Der Code beweist Struktur, nicht Bedeutung. Was eine Story fachlich meint
(„eigener Bereich", „überschaubar", „große Auswahl"), entscheidet eine
Person. Offene fachliche Fragen mit `AskUserQuestion` stellen, die Antwort
mit Datum in den Plan schreiben. Keine Annahme als Tatsache in den Plan.

## 4. Plan als Kommentar

```bash
gh issue comment <nr> -R Six7-app-store/deployment --body-file plan.md
gh issue edit <nr> -R Six7-app-store/deployment --add-label geplant
```

Form des Kommentars — jeder Abschnitt ist Pflicht, „entfällt" mit Grund ist
eine gültige Antwort:

```markdown
## Plan (Stand TT.MM.JJJJ)

**Ziel:** ein Satz, was danach anders ist.

**Abnahme:** prüfbare Kriterien, je eines mit dem Kommando oder Klickpfad,
der es beweist. Übernommen aus dem Arbeitspaket, wo es passt.

**Ist (belegt):** Fundstellen mit Datei:Zeile.

**Nicht Teil davon:** was naheliegt, aber nicht dazugehört.

**Schritte je Repo:** ein Abschnitt je Repo = ein Commit je Repo.
Branch `feat/us-NN-<kurz>` von `develop`.

**Tests:** welche neuen Tests, wo, Erfolgs- und Verweigerungsfall.

**Doku und Diagramme:** welche Dateien aus `harness/doc-map.json` betroffen
sind, welche Diagramme sich ändern. Siehe Skill `doku-nachziehen`.

**ADR:** nötig ja/nein, mit Grund (Kriterien in `adr-schreiben`).

**Risiken:** z. B. Migration auf bestehenden Daten, Staging-Neubau killt
laufende App-Deploys.

**Nachweis auf Staging:** was nach dem Merge auf `staging.<zone>` geprüft
wird und wie.

**Offene Fragen:** an wen, oder „keine".
```

**Erst bauen, wenn eine Person den Plan bestätigt hat** — in der Sitzung
oder als Kommentar im Issue. Das ist der eine Punkt, an dem ein Mensch
hinsieht, bevor Arbeit entsteht.

## 5. Danach: die Kette

| Schritt | Wer | Womit |
|---|---|---|
| Branch je Repo von `develop` | Agent | `git switch -c feat/us-NN-… origin/develop` |
| Umsetzung | Agent | Hooks halten Lint grün |
| Doku, Diagramme, ADR | Agent | `doku-nachziehen`, `adr-schreiben` |
| Commit je Repo | Agent | Commit-Gate fährt Doku-Zuordnung und Tests |
| Gegenprüfung | Agent | `/code-review`, `/pr-review` |
| Push, PR | Agent, **mit Rückfrage** | `gh pr create -R Six7-app-store/<repo> --base develop`, Body: `Teil von Six7-app-store/deployment#<nr>` |
| CI grün | Agent wartet | `gh pr checks <pr> -R Six7-app-store/<repo> --watch` |
| **Merge** | **Person** | |
| Staging gebaut, Nachweis | Agent | `make staging-status`, Abnahme auf `staging.<zone>`, Beleg als Kommentar ins Issue |
| Issue schließen | Agent schlägt vor, **Person bestätigt** | |

`Teil von` statt `Closes`: ein Merge auf `develop` heißt „auf Staging",
nicht „abgenommen". Geschlossen wird nach dem Nachweis.
