---
name: deployment-pruefen
description: Nachweisen, ob ein Deployment durchgelaufen ist und die Instanz wirklich antwortet. Aufruf mit dem Hostnamen als Argument.
---

# Deployment prüfen

Weist nach, ob eine Umgebung läuft — mit Belegen, nicht mit Behauptungen.

Aufruf: `/deployment-pruefen <hostname>` — ohne Argument wird die lokale
Dev-Umgebung geprüft.

## Die Grenze

Seit ADR 0014 gibt es zwei Stände. Die Grenze läuft zwischen ihnen
(ADR 0015):

| Stand | Adresse | Agent |
|---|---|---|
| `develop` = Staging | `staging.<zone>` | **darf neu bauen**, ohne Rückfrage |
| `main` = Produktion | `appstore.<zone>` | nur lesen |
| eigenständige Installation (`docker-compose.prod.yml`) | eigene VM, läuft derzeit nirgends | nicht anfassen |

| Erlaubt | Verboten |
|---|---|
| `gh workflow run staging.yml` für den Stand `develop` | jeder Lauf für den Stand `main` |
| `make staging-status`: DNS, Health, Pipeline beider Stände | Produktions-Deployment, `make prod*` |
| Erreichbarkeit über IPv4 und IPv6 prüfen | `tofu apply` / `destroy` von Hand |
| Pipeline-Ergebnis lesen, `gh run watch` | Rollback, `deploy.cmd`, `scripts/deploy.sh` |
| | Secrets lesen oder schreiben |

Die Grenze läuft zwischen **dem Knopf, den die Pipeline anbietet** und **der
Infrastruktur darunter**. Den Workflow starten: ja. OpenTofu von Hand gegen
OpenStack: nein. Dieselbe Regel, die auch für Menschen im Team gilt.

### Den develop-Stand neu bauen

```bash
gh workflow run staging.yml -R Six7-app-store/deployment --ref develop \
  -f stand=develop -f recreate=false
gh run list -R Six7-app-store/deployment --workflow staging.yml --limit 1
gh run watch <id> -R Six7-app-store/deployment
```

Der Bash-Hook lässt genau diese Form durch und blockt alles andere:
`stand` und `--ref` müssen beide ausdrücklich `develop` sein, und es darf kein
Staging-Deploy laufen oder warten. Lässt sich das nicht feststellen, wird
nicht ausgelöst.

**Es gibt keinen Trockenlauf.** Die Eingaben:

| Eingabe | Default | Wirkung |
|---|---|---|
| `stand` | develop | welcher Stand gebaut wird; `main` ist für den Agenten gesperrt |
| `recreate` | **true** | `tofu destroy -auto-approve`, dann Neuaufbau. Die VM ist danach eine andere Maschine mit neuer IPv6-Adresse |
| `seed` | false | legt Benutzer, Kurse und Apps an |

Der schonendste Lauf ist `recreate=false`: kein `destroy`, nur Änderungen.
`recreate=true` nur, wenn ein sauberer Neuaufbau der Zweck ist.

**Was der Hook nicht sieht:** laufende App-Deployments *innerhalb* des
Stores. Ein Neubau ersetzt den Worker; ein Deploy, der gerade auf cloud-init
wartet, bleibt danach für immer auf „läuft". Vor einem Lauf mit
`recreate=true` deshalb im Store unter Deployments nachsehen oder im Team
fragen, ob gerade jemand vorführt.

## Lokal (dev)

```bash
# Antwortet das Backend?
curl -sS http://localhost:8000/health
# erwartet: {"status":"healthy","service":"backend-api","version":"1.0.0"}

# Welche Container laufen, und sind sie gesund?
make dev-ps

# Frontend erreichbar?
curl -sS -o /dev/null -w '%{http_code}\n' http://localhost:5173/
```

Ein Dienst ohne `healthy` in `make dev-ps` ist die erste Spur. Dann die Logs:
`make dev-logs-backend`, `-worker`, `-frontend`, `-keycloak`.

## Staging

```bash
make staging-status          # im deployment-Repo; ändert nichts
```

Prüft für beide Stände: ob der lokale DNS dieselbe IPv6-Adresse kennt wie der
öffentliche, Frontend, IPv4 und IPv6 getrennt, `/api/health`, Keycloak, und
die letzten Läufe von `staging.yml`.

Meldet es „AAAA lokal … öffentlich …", ist der lokale Resolver veraltet,
nicht das Deployment: jeder Neubau vergibt eine neue IPv6-Adresse. Erst
`ipconfig /flushdns` (Windows), dann neu prüfen.

„Kein A-Eintrag" ist kein Fehler: die VMs sind nur über IPv6 erreichbar.

Eine Anfrage an die nackte IP schlägt über HTTPS **erwartungsgemäß** fehl:
Caddy wählt den Site-Block über den Namen im SNI, den eine IP nicht mitbringt.
Über HTTP antwortet sie mit `308` — das genügt als Erreichbarkeitsnachweis und
ist kein Fehler.

## Pipeline-Ergebnis

Der Staging-Deploy läuft seit `52d931f` in **GitHub Actions**, auf dem
self-hosted Runner im Campusnetz (VM `github-runner`). Der frühere
Forgejo-Workflow ist gelöscht. Warum ein eigener Runner nötig ist: die
OpenStack-API der DHBW ist von außen nicht erreichbar —
`docs/adr/0002-self-hosted-runner-auf-eigener-vm.md`.

Ausgelöst wird er von einem Push auf `develop` oder `main`, von einem
`repository_dispatch` aus der CI von frontend, backend oder worker, oder von
Hand. Der Branch des Auslösers bestimmt den Stand (ADR 0014). Der Workflow
gibt am Ende `docker compose ps` aus; dort sollten alle Dienste mit
Healthcheck `healthy` sein. Das Log ist die verlässlichste Quelle dafür,
*warum* etwas schiefging.

Sowohl das Deployment als auch der CI-Status der Pull Requests liegen damit
auf GitHub:

```bash
gh run list -R Six7-app-store/deployment --workflow staging.yml --limit 5
gh run view <id> -R Six7-app-store/deployment --log-failed
gh pr checks <nr> -R Six7-app-store/<repo>
```

An die Container-Logs auf der VM kommt der Agent nicht: Port 22 ist nur aus
dem Campusnetz offen. Was der Workflow ausgibt, ist das, was es gibt.

**Voraussetzung:** `gh` muss installiert und angemeldet sein. Ist es das nicht,
schlagen diese Befehle mit `command not found` fehl — dann ist das die
Antwort, nicht ein rotes Deployment.

## Wenn etwas rot ist

Reihenfolge, die am schnellsten zur Ursache führt:

1. **Antwortet der Host überhaupt?** Nein → Infrastruktur oder DNS, nicht der
   Code.
2. **Antwortet er, aber mit 502/503?** → Container läuft nicht oder ist nicht
   healthy. Compose-Status und Logs ansehen.
3. **200, aber die Anwendung verhält sich falsch?** → Deployment war
   erfolgreich, das Problem liegt im Code. Ab hier gilt der normale Weg:
   reproduzieren, Test schreiben, beheben.

Ein häufiger Fall, der wie ein kaputtes Deployment aussieht und keiner ist:
eine geänderte `.env` wurde mit `restart` statt `up -d --force-recreate`
übernommen. Die Umgebung wird beim **Erzeugen** des Containers festgeschrieben —
ein Neustart liest sie nicht neu.

## Ergebnis melden

Nicht „Deployment ok" behaupten, sondern zeigen: das Kommando, den Statuscode,
die Antwort des Health-Endpunkts. Ein Beleg ist schneller zu prüfen als eine
Behauptung nachzustellen.
