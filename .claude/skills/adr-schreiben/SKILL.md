---
name: adr-schreiben
description: Wann ein Architecture Decision Record fällig ist und wie er aussieht — Format, Nummernvergabe, und die Regel, dass ein ADR nie nachträglich geändert wird.
---

# ADR schreiben

Ein ADR hält fest, **warum** etwas so ist. Der Code zeigt das Was. Besonders
wichtig für die Fälle, in denen etwas bewusst *nicht* gebaut wurde — die sind
im Code nämlich unsichtbar.

## Wann fällig

Sobald eine Entscheidung eine dieser Eigenschaften hat:

- teuer zurückzunehmen (Datenmodell, Schnittstelle, Betriebsmodell)
- schließt eine naheliegende Alternative aus
- jemand wird in sechs Monaten fragen „warum eigentlich nicht einfach …?"

Kleine Entscheidungen brauchen keins — ein Kommentar im Code reicht. Im Zweifel
hilft die Frage: Würde jemand, der das später liest, die Entscheidung für einen
Fehler halten, ohne den Kontext zu kennen?

## Wo

`deployment/docs/adr/NNNN-kurzer-titel.md`. Fortlaufend nummeriert, Nummern
werden nie wiederverwendet. Die nächste freie Nummer ist die höchste Datei
unter `docs/adr/` plus eins — nicht die letzte Zeile im Verzeichnis, das
kann hinterherhinken. Vorher die offenen Branches prüfen, damit zwei
Arbeiten nicht dieselbe Nummer vergeben:

```bash
git fetch -q origin
for b in $(git branch -r --format='%(refname:short)'); do
  git ls-tree --name-only "$b" docs/adr/ 2>/dev/null
done | grep -oE '[0-9]{4}' | sort -u | tail -3
```

## Format

```markdown
# NNNN — Titel im Aussagesatz

**Status:** Vorschlag | Angenommen | Abgelöst durch ADR-NNNN
**Datum:** TT.MM.JJJJ
**Beteiligt:** wer die Entscheidung getragen hat

## Kontext
Was war der Fall, als die Entscheidung anstand. Fakten, keine Wertung.

## Entscheidung
Ein Satz im Aktiv: „Wir tun X."

## Konsequenzen
Was dadurch leichter wird — und was schwerer. Beides.

## Verworfene Alternativen
Je Alternative: was sie gewesen wäre und woran sie gescheitert ist.
```

## Die drei Regeln, an denen es meistens scheitert

**1. Kontext ohne Wertung.** Im Kontext stehen Fakten: Zahlen, Einschränkungen,
Aussagen aus Gesprächen mit Datum. Die Bewertung gehört in „Entscheidung". Wer
den Kontext schon wertend schreibt, produziert eine Begründung im Nachhinein
statt einer nachvollziehbaren Ableitung.

**2. Konsequenzen in beide Richtungen.** Ein ADR, in dem nur Vorteile stehen,
ist eine Werbebroschüre. Was wird schwerer? Was geht jetzt nicht mehr? Genau
das macht später den Unterschied, wenn jemand die Entscheidung überprüft.

**3. Verworfene Alternativen mit dem Grund des Scheiterns.** Nicht „Wine wurde
verworfen", sondern was Wine gewesen wäre und woran genau es scheiterte. Das
ist der Teil, der verhindert, dass in einem Jahr jemand dieselbe Option erneut
prüft.

## Nicht nachträglich ändern

Ein ADR wird nach dem Schreiben **nicht mehr bearbeitet**. Ändert sich die
Lage, entsteht ein neues, und das alte bekommt den Status „Abgelöst durch
ADR-NNNN". Die Historie ist der Punkt an der Sache — ein nachträglich
geglättetes ADR erzählt nur noch, was man heute denkt.

## Vorbild

`docs/adr/0001-keine-windows-apps.md` zeigt das Format an einem echten Fall,
inklusive vier verworfener Alternativen mit Begründung.

## Danach: Verzeichnis, Doku und Diagramme nachziehen

Ein ADR ändert selten nur sich selbst. Was eine Entscheidung festlegt, steht
meist schon irgendwo anders als Beschreibung, Tabelle oder Diagramm — und ist
ab jetzt falsch. Im **selben Commit**:

1. **Verzeichnis.** Zeile in `docs/adr/README.md` mit Nummer, Titel und
   Status. Der Status muss Wort für Wort dem im ADR entsprechen;
   `make docs-check` vergleicht beides.
2. **Abgelöstes ADR.** Ersetzt das neue ein altes, ändert sich dort genau
   eine Zeile: `**Status:** Abgelöst durch ADR-NNNN`. Dazu die Zeile im
   Verzeichnis. Sonst nichts am alten ADR.
3. **Doku und Diagramme.** Skill `doku-nachziehen`: über
   `harness/doc-map.json` und eine Suche nach den Begriffen der Entscheidung
   jede Stelle finden, die das Alte beschreibt — Fließtext, Tabellen,
   Mermaid-Diagramme in `docs/architektur.md`, die `AGENTS.md` der Repos.
4. **Prüfen.** `make docs-check` grün. Der Check findet fehlende
   Verzeichniseinträge, abweichende Status, tote Links und Diagramme, die
   nicht mehr rendern — nicht aber eine Beschreibung, die inhaltlich
   veraltet ist. Die findet nur das Lesen in Schritt 3.

Kommt die Entscheidung aus einem Issue, gehört ein Verweis auf das ADR in
den Issue-Kommentar zum Plan (`/issue-planen`).

## Pflicht

Die ADR-Pflicht steht in der Definition of Done aller vier `AGENTS.md`. Ein ADR
gehört in **denselben Commit** wie die Entscheidung, die es beschreibt — sonst
wird es nie geschrieben.
