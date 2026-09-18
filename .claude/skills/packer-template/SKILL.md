---
name: packer-template
description: Welches Packer-Layout ein App-Repository haben muss, damit der Worker es findet — einzelnes Template oder Unterordner, und warum beides gleichzeitig abgelehnt wird.
---

# Packer-Layout eines App-Repositories

Der Worker klont das App-Repo am Release-Tag und sucht darin nach
Packer-Templates. `app/services/packer_discovery.py` bestimmt, was gefunden
wird. Laden, wenn ein App-Repo eingerichtet wird oder ein Deployment mit
„kein Template gefunden" bzw. „Layout mehrdeutig" abbricht.

## Die zwei erlaubten Layouts

**Einzelnes Template** — eine App, ein Image:

```
packer/
├── template.pkr.hcl
└── variables.pkr.hcl      # optional
```

Der Schlüssel dieses Templates ist `default`.

**Mehrere Templates** — eine App, mehrere Varianten:

```
packer/
├── klein/
│   ├── template.pkr.hcl
│   └── variables.pkr.hcl  # optional
├── gross/
│   └── template.pkr.hcl
└── _common/
    └── scripts/…          # wird ignoriert
```

Der Schlüssel ist jeweils der Ordnername. Ordner **ohne** `template.pkr.hcl`
werden stillschweigend übergangen — genau dafür ist `_common` da: gemeinsame
Skripte, die mehrere Templates einbinden.

## Warum beides zusammen abgelehnt wird

Liegt `packer/template.pkr.hcl` **und** mindestens ein Unterordner mit einem
Template vor, bricht die Discovery mit einem Fehler ab, statt sich für eines zu
entscheiden.

Das ist Absicht. Es gibt keine Reihenfolge, die nicht willkürlich wäre, und ein
Deployment, das das falsche Image baut, fällt erst auf, wenn die VM läuft. Ein
klarer Abbruch beim Klonen ist billiger als ein falsches Image in Produktion.

**Auflösung:** Entweder die Datei auf oberster Ebene entfernen und alles in
Unterordner legen, oder die Unterordner auflösen. Nicht beides behalten.

## Variablen

`variables.pkr.hcl` ist optional. Der Aufrufer prüft mit `os.path.isfile`, ob
es existiert — nicht jedes Template deklariert Variablen. Wenn du Code
schreibst, der die Datei liest: dieselbe Prüfung vorher machen, sonst fliegt es
bei Templates ohne Variablen.

## Was der Worker damit tut

1. App-Repo am Release-Tag klonen
2. Templates finden (dieser Schritt)
3. Bei Bedarf `packer build` — das Image entsteht in OpenStack
4. `terraform apply` mit den Variablen des Deployments

Die OpenStack-Credentials kommen **pro Task** als eigene `clouds.yaml` aus dem
vom Backend verschlüsselten Envelope. Nie zwischen Tasks teilen, nie ins Log.

## Tests

Echte Packer- oder OpenStack-Aufrufe gehören nicht in die Suite — sonst baut
sie Ressourcen, die niemand abräumt. Alles mocken.

```bash
docker exec worker-dev poetry run pytest
```

Der Per-Test-Timeout steht auf 60 Sekunden mit `--timeout-method=thread`. Die
Methode ist Pflicht: `signal` kann einen hängenden `subprocess.wait()` nicht
unterbrechen, und genau den produziert dieser Worker, wenn ein Mock durchrutscht.
