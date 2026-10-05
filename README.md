# Worker

[![Coverage](https://img.shields.io/endpoint?url=https://six7-click-n-deploy.github.io/worker/badge.json)](https://six7-click-n-deploy.github.io/worker/)

Celery-Worker des App Stores. Konsumiert Deployment-Tasks aus RabbitMQ, klont das App-Repository, führt OpenTofu aus und provisioniert auf OpenStack.

## Setup

Dieses Repository wird nicht eigenständig gestartet. Der Worker braucht RabbitMQ, Redis, die Postgres-tfstate-DB und vom Backend dispatchte Tasks — der gesamte Stack wird über das deployment-Repository hochgefahren. Vollständige Anleitung: [deployment/README.md](https://github.com/six7-click-n-deploy/deployment#readme).

Voraussetzung für alle folgenden Befehle: `make dev-up` aus dem `deployment/`-Verzeichnis wurde ausgeführt und der Stack läuft.

## Entwicklung

Alle `make`-Befehle werden aus dem `deployment/`-Verzeichnis des [deployment-Repos](https://github.com/six7-click-n-deploy/deployment) ausgeführt — dort liegt das Makefile.

```bash
# in app-store/deployment
make dev-restart-worker   # Worker neu starten (z. B. nach Änderung an tasks.py)
make dev-logs-worker      # Worker-Logs verfolgen
make shell-worker         # interaktive Shell im Container
```

Tests, Lint und Format laufen im Worker-Container — `make shell-worker` öffnet eine Shell, in der `poetry run pytest`, `poetry run ruff check` und `poetry run ruff format` zur Verfügung stehen.

## Was der Worker tut

- **Deploy**: klont das App-Repo am Release-Tag, führt `tofu apply` aus. Ein Image wird nicht gebaut: die VM richtet sich beim Boot über cloud-init (`user_data` im `tofu/`-Code der App) selbst ein
- **Destroy**: `tofu destroy` gegen denselben Tag/dieselben Variablen
- **Update**: deployt neue Version im Bestands-State
- **OpenStack-Auth**: per-Task `clouds.yaml`, generiert aus dem vom Backend verschlüsselten Credentials-Envelope

## Technologie-Stack

- **Celery 5** mit RabbitMQ als Broker, Redis als Result-Backend
- **OpenTofu 1.x** mit Postgres-Remote-State
- **GitPython** für Repo-Klone
- **SQLAlchemy 2.0** nur lesend gegen die App-DB
- **pytest** mit `unit` und `integration` als Markern

## App-Vertrag

Der Worker erwartet im App-Repo genau ein Verzeichnis `tofu/` mit `*.tofu`-Dateien.
Ein Repo, das noch `packer/` oder `terraform/` mitbringt, lehnt er mit einer
Fehlermeldung ab, die den nötigen Umbau nennt (`_resolve_tofu_dir` in `tasks.py`).
Hintergrund: ADR 0010 im deployment-Repo.

## Code-Struktur

Der Code liegt in `app/`. Einstieg ist `tasks.py`: Celery ruft eine Task-Funktion auf, die die Services orchestriert — Repo klonen → OpenTofu → OpenStack. Jeder Service kapselt genau einen dieser Schritte.

```
app/
├── celery_app.py    # Celery-Instanz + Config (Broker, Result-Backend, Serializer)
├── config.py        # Pydantic-Settings aus Env-Variablen
├── tasks.py         # Die Celery-Tasks + Failure-Exception; orchestriert die Services
├── services/        # Ein Service pro Deploy-Schritt (siehe unten)
└── utils/           # crypto (Envelope-Entschlüsselung), logger (strukturiertes Logging)
```

**tasks.py** definiert fünf Celery-Tasks — jeder orchestriert die Services für seinen Ablauf:

| Task | Zweck |
|---|---|
| `tasks.deploy_application` | Repo klonen → `tofu plan` → `tofu apply` |
| `tasks.destroy_deployment` | `tofu destroy` gegen denselben State |
| `tasks.pause_deployment` | VMs stoppen (Daten bleiben) |
| `tasks.resume_deployment` | Pausierte VMs wieder starten |
| `tasks.redeploy_resource` | Einzelne Ressource im Bestands-State neu ausrollen |

Die `Failure`-Exception in `tasks.py` trägt strukturierte Fehlerdaten durch Celery zurück, damit das Backend sie dem User anzeigen kann.

**services/** — je ein Schritt der Pipeline:

| Service | Zweck |
|---|---|
| `git_service` | Klont das App-Repo am Release-Tag (HTTPS + Token) |
| `tofu_executor` | Führt `tofu init/plan/apply/destroy` aus (Postgres-Remote-State) |
| `openstack_auth` | Materialisiert per-Task `clouds.yaml` aus dem verschlüsselten Credentials-Envelope |
| `openstack_service` | VMs stoppen/starten für Pause und Resume |

## Mehr

- Architektur und projektübergreifende Doku: [.github-Repo](https://github.com/six7-click-n-deploy/.github)
- Backend-Service: [backend-Repo](https://github.com/six7-click-n-deploy/backend)
