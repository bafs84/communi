# Direkter Zugriff auf das Communi Live-System

Basisadresse: `https://ckw.communi.ch/v1/ckw`. Die API verwendet dieselben Projekte,
verschlüsselten Dateien, Kontingente und Berechnungen wie der CKW-Arbeitsbereich.
Sie erlaubt keine Shell-Befehle, SQL-Abfragen oder beliebigen Dateisystemzugriffe.

## Zugang einrichten

1. Unter `https://communi.ch/raum/ckw/` anmelden und oben **API-Zugänge** öffnen.
2. Einen Namen, eine Gültigkeit und die benötigten Rechte wählen. Für ein
   bestehendes Projekt den Zugang möglichst auf dieses Projekt begrenzen.
   Die Voreinstellung erlaubt Lesen, Bearbeiten/Hochladen und Berechnen.
   Löschen ist ein eigenes, zunächst ausgeschaltetes Recht.
3. Den einmal angezeigten Schlüssel kopieren. Lokal im Repository ausführen:

   ```bash
   python3 scripts/communi-api.py configure
   python3 scripts/communi-api.py info
   python3 scripts/communi-api.py projects
   ```

`configure` liest den Schlüssel verdeckt und speichert ihn mit privaten
Dateirechten in `runtime/communi-api-token` (von Git ausgeschlossen). Alternativ
`COMMUNI_API_TOKEN` aus einem Secret Store bereitstellen oder vor dem Befehl
`--token-file /privater/pfad` angeben. Schlüssel gehören weder in Chatnachrichten
noch in Kommandozeilenargumente, Git, URLs oder Logs. `COMMUNI_API_URL` bzw.
`--url` ändern die Basisadresse; HTTP ist ausschließlich auf Loopback erlaubt.
Der Client folgt keinen Redirects und wiederholt Schreibaufrufe nicht automatisch.

Alle Schlüssel sind an das ausstellende Konto gebunden. Sie gelten 1 bis maximal
365 Tage (Standard 30); maximal 20 gleichzeitig aktive Schlüssel pro Konto.
Widerruf im Portal sperrt neue Aufrufe sofort. Bereits laufende Aufrufe können
noch enden. Bei Projektbindung zeigt die Projektliste nur dieses Projekt,
weitere Projekte können damit nicht angelegt werden. Löschen des gebundenen
Projekts entfernt auch den Schlüssel. Schlüssel können keine weiteren Schlüssel
ausstellen oder verwalten und funktionieren nur an den freigegebenen `/v1/ckw`
Routen. Auth0 bleibt für die Schlüsselverwaltung erforderlich.

## Arbeiten mit dem Client

```bash
python3 scripts/communi-api.py create --name 'Analyse' --municipality 'Sursee'
# Die project_id aus der Antwort verwenden:
python3 scripts/communi-api.py upload PROJECT_UUID --kind consumption /pfad/verbrauch.csv
python3 scripts/communi-api.py upload PROJECT_UUID --kind solar /pfad/pv.csv
python3 scripts/communi-api.py files PROJECT_UUID
python3 scripts/communi-api.py run PROJECT_UUID summary
python3 scripts/communi-api.py run PROJECT_UUID calculation --parameters '{"tariffYear":2026}'
python3 scripts/communi-api.py run PROJECT_UUID vzev-simulation
python3 scripts/communi-api.py run PROJECT_UUID excel-export --output /privat/auswertung.xlsx
python3 scripts/communi-api.py results PROJECT_UUID
python3 scripts/communi-api.py download PROJECT_UUID --result RESULT_UUID --output /privat/export.xlsx
python3 scripts/communi-api.py download PROJECT_UUID --file FILE_UUID --output /privat/original.csv
```

JSON-Parameter können auch mit `--parameters @/pfad/parameter.json` eingelesen
werden. `get` liest die Projektkonfiguration. `update --config @datei.json`
ersetzt die **gesamte** Konfiguration, daher zuerst `get` verwenden und alle
beizubehaltenden Felder übernehmen. JSON-Auswertungen werden direkt ausgegeben;
Binärexporte benötigen `--output` und werden zusätzlich verschlüsselt in der
Resultathistorie gespeichert. Bestehende lokale Ausgabedateien werden nicht
überschrieben. Nach einem Timeout vor erneutem Export die Historie prüfen.
Ein Upload speichert zunächst die Datei; Format- und fachliche Prüfungen erfolgen
beim Aufruf der Berechnungsengine. Dateilisten liefern SHA-256-Prüfsummen für den
Abgleich mit dem Original.

`delete-file PROJECT_UUID FILE_UUID` und `delete-project PROJECT_UUID` löschen
dauerhaft und benötigen das zusätzliche Recht `delete`.

## HTTP-Vertrag

Jeder direkte Aufruf benötigt `Authorization: Bearer communi_api_<secret>`.
Cookies und CSRF-Token sind bei API-Schlüsseln nicht erforderlich. Schlüssel
werden ausschließlich als SHA-256-Digest gespeichert; die Verwaltungsantworten
enthalten nur Metadaten und einen kurzen Erkennungspräfix. Antworten tragen
`Cache-Control: private, no-store`.

| Methode und Pfad (relativ zur Basisadresse) | Recht | Inhalt |
| --- | --- | --- |
| `GET /capabilities` | `read` | Operationen, Uploadarten und Größenlimits |
| `GET /municipalities` | `read` | Gemeinden |
| `GET /projects` | `read` | Eigene bzw. gebundene Projekte |
| `POST /projects` | `write` | JSON: `name`, `municipality` |
| `GET /projects/{project_id}` | `read` | Projekt und Konfiguration |
| `PATCH /projects/{project_id}` | `write` | JSON: optional `name`, `municipality`, `config` |
| `DELETE /projects/{project_id}` | `delete` | Projekt samt Dateien und Resultaten |
| `POST /projects/{project_id}/uploads/{kind}` | `write` | Multipart, ein oder mehrere Felder `files` |
| `GET /projects/{project_id}/files` | `read` | Dateien, Prüfsummen, Downloadpfade |
| `GET /projects/{project_id}/files/{file_id}/download` | `read` | Originaldatei |
| `DELETE /projects/{project_id}/files/{file_id}` | `delete` | Datei entfernen |
| `POST /projects/{project_id}/operations/{operation}` | `calculate` | JSON: `{"parameters": {...}}` |
| `GET /projects/{project_id}/results` | `read` | Gespeicherte Exporte |
| `GET /projects/{project_id}/results/{result_id}/download` | `read` | Export herunterladen |

Uploadarten: `solar`, `consumption`, `zev`, `billing`. Standard: 10 MiB pro Datei,
1 GiB gespeicherte Dateien/Resultate pro Konto. Der Standardclient begrenzt Batches
auf 50 MiB; individuelle Serverlimits stehen in `/capabilities`.

Operationen: `summary`, `timeseries`, `plants`, `calculation`, `residual-solar`,
`optimization`, `battery-placements`, `load-shifting`, `vzev-simulation`,
`vzev-simulation-csv`, `vzev-tariff-comparison-csv`, `leg-word-report`,
`vzev-word-report`, `excel-export`, `billing-invoices`.
Parameter entsprechen dem CKW-Arbeitsbereich und werden unter `parameters`
übergeben, z. B. `tariffYear`, `networkDiscount`, `legEnergyPriceRpPerKwh`,
`fixedPvTariffRpPerKwh`, `kind`, `resolution`. Für `billing-invoices` ist bei jedem
Generierungsaufruf eine bestätigte Tarifklasse in `parameters.tariffTier` nötig:
`lt_50000` oder `gt_50000`. Fehlende Werte, `auto` und unbekannte Klassen werden
mit HTTP 400 abgewiesen. Die Klasse gilt für alle Monate des Batches; die
Monatswerte ändern sie nicht. Weitere Abrechnungsparameter werden ebenfalls
unter `parameters` übergeben. Die Berechnung läuft
synchron; Binärantworten enthalten `X-Result-Id` und `Content-Disposition`.

```bash
python3 scripts/communi-api.py upload PROJECT_UUID --kind billing /pfad/monat.csv
python3 scripts/communi-api.py run PROJECT_UUID billing-invoices --parameters '{"tariffTier":"gt_50000","year":2026}' --output /privat/rechnungen.zip
```

Das Formular `POST /api/vzev/invoices` verwendet dafür `tariff_tier`. Bestehende
Clients müssen dieses Feld künftig mitsenden. Die Rechnungs-CLI verlangt
`--tariff-tier lt_50000` oder `--tariff-tier gt_50000`. Eine in `config.billing`
gespeicherte frühere Auswahl ersetzt die Bestätigung im neuen Aufruf nicht.

Monats-JSON enthalten `selected_tariff_tier`, `tariff_year`,
`tariff_basis_source` und `tariff_selection_source: "contract_confirmed"`.
Der Batch-Index enthält zusätzlich die Liste `tariff_years` und dieselben
Angaben je Periode. Word-Ausgaben kennzeichnen die Klasse als „vertraglich
gewählt“. „CKW-Kostenanteil“ bezeichnet die Bezugskosten inklusive MWST und
Rundung; Rückliefergutschriften werden separat berücksichtigt. Bestehende
Exporte bleiben erhalten; neue Berechnungen erzeugen neue Ergebnisse.

Fehler: `400` ungültige Eingabe, `401` fehlender/ungültiger/abgelaufener/
widerrufener Schlüssel, `403` fehlendes Recht oder Projektbindung verletzt,
`404` unbekannte oder fremde Ressource, `413` Größenlimit, `507` Kontingent,
`502` Berechnungsengine nicht verfügbar.

## Schlüsselverwaltung mit Auth0

Nur mit gültigem Auth0-Benutzertoken:

- `GET /api-keys`: letzte 100 eigene Schlüssel, ohne Secret oder Digest.
- `POST /api-keys`: JSON `{"name":"Codex","scopes":["read","write","calculate"],"expires_in_days":30,"project_id":null}`;
  Status `201`, Metadaten und einmalig `token`.
- `DELETE /api-keys/{key_id}`: eigenen Schlüssel widerrufen, Status `204`.

Die Migration `003_api_keys.sql` ist additiv und wird beim API-Start angewendet.
Im Git-basierten Portainer-Stack den getesteten Commit neu bauen und ausrollen;
Volumes und bestehende Schlüssel beibehalten. API und Gateway müssen aktualisiert
werden (Gateway enthält die Portaloberfläche). Es sind keine neuen externen
Secrets oder zusätzlichen Infrastrukturservices erforderlich.
