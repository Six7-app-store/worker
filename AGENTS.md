# Worker — Click-n-Deploy App Store

Celery-Worker. Klont App-Repos, baut bei Bedarf Packer-Images und fährt
Terraform gegen OpenStack. Läuft nur als Service im Stack aus
`deployment/docker-compose.dev.yml`.

## Befehle

- Tests: `docker exec worker-dev poetry run pytest`
- Einzelne Datei: `docker exec worker-dev poetry run pytest tests/test_x.py --no-cov`
- Lint (alle drei, so fährt es auch die CI):
  - `docker exec worker-dev poetry run ruff check .`
  - `docker exec worker-dev poetry run black --check .`
  - `docker exec worker-dev poetry run isort --check-only .`
- Logs: `docker logs -f worker-dev`
- Nach Änderung an `app/tasks.py`: `make dev-restart-worker` (im `deployment/`-Repo)

**Immer `poetry run` im Container.** Das venv liegt unter `/app/.venv` und ist
nicht im `PATH` — ein blankes `pytest` scheitert mit `No module named pytest`.

## Konventionen

- Jeder Task holt seine OpenStack-Credentials als eigene `clouds.yaml` aus
  dem vom Backend verschlüsselten Envelope. Credentials nie im Klartext
  loggen und nie zwischen Tasks teilen.
- Externe Kommandos (git, packer, terraform) laufen über die Executor-Module
  in `app/services/`, nicht per `subprocess` direkt im Task.
- Packer-Layouts: entweder `packer/template.pkr.hcl` (einzeln) oder
  `packer/<key>/template.pkr.hcl` (mehrere). Beides gleichzeitig ist ein
  Fehler und wird bewusst abgelehnt statt geraten — siehe `packer_discovery.py`.
- Tests laufen mit `--timeout=60` und `--timeout-method=thread`. Die Methode
  ist Pflicht: `signal` kann hängende subprocess-Waits nicht unterbrechen,
  und genau die produziert dieser Worker.

## Definition of Done

- `ruff check .`, `black --check .`, `isort --check-only .` und `pytest` grün
- Coverage nicht gesunken (`fail_under` in `pyproject.toml`)
- Architekturentscheidung getroffen? ADR unter `deployment/docs/adr/`

## Nicht anfassen

- `.env` und alles auf `*.pem`
- Kein Prod-Deploy, kein `git push --force`
- Keine echten OpenStack-Calls in Tests — alles gemockt, sonst baut die
  Suite Ressourcen, die niemand abräumt
- `.claude/` — erzeugt aus `deployment/harness/`. Was hier geändert wird, ist
  beim nächsten `make harness-sync` weg. Änderungen gehören in die Quelle.

Geheimnisse, Produktions-Deploys, `terraform apply` und Pushes auf `main` sind
zusätzlich als deny-Regel in `.claude/settings.json` gesperrt. So ein Kommando
scheitert ohne Nachfrage — das ist Absicht und kein Werkzeugfehler.
