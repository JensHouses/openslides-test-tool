# openslides-test-tool – Last- und Performancetest für OpenSlides-Abstimmungen

Python-Tool, das den kritischsten Pfad von OpenSlides 4 testbar macht: **elektronische
Wahlen und Abstimmungen über Anträge**, auch mehrere gleichzeitig.

Das Tool basiert auf [OpenSlides/openslides-performance](https://github.com/OpenSlides/openslides-performance)
(Go) und dem Fork von [ostcar](https://github.com/ostcar/openslides-performance)
(Branch `feature/vote`, angepasst an den neuen Vote-Service). Die Verbindungsoptionen
sind kompatibel (`-d`, `-u`, `-p`, `--http`, `-4`). Anders als dort musst du hier nichts
vorbereiten: Das Tool legt alles selbst an und entfernt es am Ende wieder.

## Ablauf

```
 admin_login ─ preflight ─ create_users ─ login_users
      │
      └─ pro Runde: create_content ─ create_polls ─ observer_connect
                    ── [ENTER] ──
                    refresh_tokens ─ start_polls ─ start_visible
                    voting (alle User × alle Polls)
                    stop_and_results ─ results_visible_all ─ Validierung
                    ── [ENTER] = nächste Runde / [q] = Ende ──
      │
      └─ cleanup (Polls, Anträge/Wahlen, User, Einstellungen)
```

1. **Login** des Superusers und **Preflight**: Meeting vorhanden? Delegierten-Gruppe?
   Elektronische Abstimmung aktiv? Welche Vote-API läuft (classic oder neu)?
2. **n User anlegen** (alle mit demselben Passwort, in der Delegierten-Gruppe, als anwesend
   markiert) und **alle einloggen**.
3. Pro Runde **P Anträge bzw. Wahlen mit je einer Abstimmung** anlegen (`--parallel P`).
4. **Warten auf ENTER**. Die Polls sind jetzt schon in OpenSlides sichtbar, du kannst
   also z. B. Browser oder Monitoring vorbereiten.
5. Alle Polls **gleichzeitig starten**, danach geben **alle User in allen Polls** ihre
   Stimme ab.
6. Sind alle Stimmen abgegeben, werden alle Polls **beendet**. Gemessen wird, wie lange
   der Stop-Request dauert und **wann das Ergebnis per Autoupdate sichtbar ist**.
7. **Validierung**: Das Ergebnis des Servers wird mit den tatsächlich abgegebenen Stimmen
   verglichen. Abweichungen werden als Fehler gemeldet (Exit-Code 2).
8. **Beenden mit `q`, Ctrl+C oder SIGTERM**: Alles Angelegte wird gelöscht und geänderte
   Einstellungen werden zurückgesetzt.

Jeder Schritt wird gemessen. Einzelne Requests (Logins, Stimmen, Starts, Stops) werden
mit ihrer Latenz protokolliert.

## Installation

Voraussetzung ist Python ≥ 3.11. Mit [uv](https://docs.astral.sh/uv/):

```bash
uv venv && uv pip install -e .
```

Oder mit pip:

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

Danach steht `openslides-test-tool` (bzw. `.venv/bin/openslides-test-tool`) zur Verfügung,
kurz auch als `os-test`.

## Benutzung

Erst prüfen, ob alles passt. Dabei wird nichts angelegt:

```bash
openslides-test-tool check -d openslides.example.org -u superadmin -m 1
```

Wahl mit 500 Usern:

```bash
openslides-test-tool run -d openslides.example.org -u superadmin -m 1 -n 500
```

Drei Wahlen parallel:

```bash
openslides-test-tool run -d openslides.example.org -m 1 -n 500 -P 3 -t assignment
```

Antragsabstimmung statt Wahl:

```bash
openslides-test-tool run -d openslides.example.org -m 1 -n 500 -t motion
```

Gemischt, zwei Wahlen und zwei Anträge parallel:

```bash
openslides-test-tool run -d openslides.example.org -m 1 -n 500 -P 4 -t assignment,motion
```

Vollautomatisch (z. B. für CI), 3 Runden ohne Rückfrage:

```bash
openslides-test-tool run -d openslides.example.org -m 1 -n 200 -P 2 --no-wait --rounds 3
```

Realistischer: Stimmen über 30 s verteilen, höchstens 200 gleichzeitig:

```bash
openslides-test-tool run -d openslides.example.org -m 1 -n 1000 --vote-spread 30 --vote-concurrency 200
```

Lokale Dev-Instanz:

```bash
openslides-test-tool run -d localhost:8000 -m 1 -n 50
```

Wenn `-p` fehlt, liest das Tool das Passwort aus der Umgebungsvariable `OS_TEST_TOOL_PASSWORD`
oder fragt danach. So landet es nicht in der Shell-History.

### Wichtige Optionen

| Option | Bedeutung | Default |
|---|---|---|
| `-d/--domain` | Server (`host:port` oder volle URL) | `localhost:8000` |
| `-u/--username`, `-p/--password` | Superuser | `superadmin` / Prompt |
| `--http` | http statt https | aus |
| `-m/--meeting` | Meeting-ID (Pflicht) | – |
| `-n/--amount` | Anzahl Test-User (= Wähler) | 10 |
| `--users-password` | gemeinsames Passwort der Test-User | `pass` |
| `--user-prefix` | Präfix der Usernamen | `perf<HHMMSS>_` |
| `-t/--target` | `assignment` (Wahl), `motion` (Antrag) oder beides mit Komma | `assignment` |
| `-P/--parallel` | Anzahl gleichzeitiger Polls | 1 |
| `--candidates` | Anzahl Kandidierende pro Wahl (aus den Test-Usern) | 3 |
| `--poll-type` | `secret` (geheim/pseudoanonym), `named`, `open` (nur neue API) | `secret` |
| `--vote-choice` | `random`, `yes`, `no`, `abstain`, `first` | `random` |
| `--publish` | Polls beim Beenden veröffentlichen | aus |
| `--rounds` | Anzahl Runden; 0 = interaktiv bis `q` | 0 |
| `--no-wait` | nicht auf ENTER warten | – |
| `--wait-before-stop` | auch vor dem Beenden der Polls auf ENTER warten | – |
| `--vote-concurrency` | max. gleichzeitige Vote-Requests (0 = alle sofort) | 0 |
| `--vote-spread` | Stimmen zufällig über X Sekunden verteilen | 0 |
| `--login-concurrency` | max. gleichzeitige Logins | 50 |
| `--api` | `auto`, `classic` (4.0–4.3) oder `new` (neuer Vote-Service) | `auto` |
| `--enable-electronic-voting` | elektronische Abstimmung bei Bedarf einschalten (wird danach zurückgesetzt) | aus |
| `--keep` | nichts löschen (später `openslides-test-tool cleanup`) | aus |
| `--config` | JSON-Datei mit Defaults, siehe `config.example.json` | `./config.json` |

Die vollständige Liste zeigt `openslides-test-tool run --help`.

## Protokoll

Jeder Lauf legt ein Verzeichnis `runs/<datum>-<zeit>_m<meeting>_n<user>_p<parallel>/` an:

| Datei | Inhalt |
|---|---|
| `protocol.log` | lesbares Protokoll, wie auf der Konsole, plus Zusammenfassung |
| `events.jsonl` | ein JSON-Objekt pro Schritt, Statistik, Validierung und Bedienereingabe |
| `requests.csv` | jeder einzelne Request: Art, Runde, Poll, User, Sendezeitpunkt, Latenz, Status, Fehler |
| `summary.json` | alle Schritte, Latenz-Statistiken (p50/p90/p95/p99/max, Durchsatz) und Validierung |
| `state.json` | alles, was angelegt oder geändert wurde (Grundlage für das Aufräumen) |

Gemessene Schritte:

| Schritt | Was gemessen wird |
|---|---|
| `create_users`, `login_users` | Anlegen und Login aller User (Login ist wegen argon2 teuer) |
| `create_content`, `create_polls` | Anlegen der Anträge/Wahlen und Polls |
| `start_polls` / `start_visible` | Start-Requests / bis der Status „gestartet“ per Autoupdate ankommt |
| `voting` | erste bis letzte Stimme, plus Latenz-Statistik pro Poll und gesamt |
| `stop_and_results` | Stop-Request und Warten auf das Ergebnis |
| `R<n> stop request` / `R<n> result visible` | pro Poll: Dauer des Stop-Requests / Zeit vom Stop bis das Ergebnis sichtbar ist |
| `results_visible_all` | erster Stop bis zum letzten sichtbaren Ergebnis |
| `cleanup_*` | Aufräumen |

„Sichtbar“ heißt: Das Tool hält wie ein Browser eine Autoupdate-Verbindung offen
(`/system/autoupdate`) und misst, wann die Änderung dort ankommt. Fällt der Stream aus,
liest es ersatzweise alle 2 s per `?single=1`.

## Aufräumen

- Normales Ende (`q`), Fehler, Ctrl+C und SIGTERM löschen automatisch Polls,
  Anträge/Wahlen und User und setzen Einstellungen zurück.
- Ein zweites Ctrl+C bricht hart ab. Ist danach noch etwas übrig, gilt das auch nach
  einem Absturz oder mit `--keep`:

  ```bash
  openslides-test-tool cleanup -d openslides.example.org -u superadmin --state runs/<lauf>/state.json
  ```

## Classic- vs. neue Vote-API

| | classic (OpenSlides 4.0–4.3) | neu (`openslides-vote-service` Branch `feature/vote`) |
|---|---|---|
| Poll anlegen/starten/stoppen | Backend-Actions `poll.create/start/stop/publish/delete` | `POST /system/vote/poll/`, `…/{id}/start`, `…/{id}/finalize`, `DELETE …/{id}` |
| Stimme | `POST /system/vote?id=<poll>` mit `{"value":{"<option>":"Y"}}` | `POST /system/vote/poll/<id>/vote` mit `{"value":"yes"}` bzw. `{"value":[<option>]}` |
| Antrag | `pollmethod YNA` | `method approval` |
| Wahl | `pollmethod Y`, 1 Stimme | `method selection`, 1 Option |
| Ergebnis | `poll/votescast`, `option/*/yes,no,abstain` | `poll/result` (JSON) |

`--api auto` erkennt die Variante: Der klassische Vote-Service antwortet auf
`POST /system/vote/poll/` mit 404.

## Hinweise für große Tests

- **Dateideskriptoren**: Bei vielen Usern vorher `ulimit -Sn 65536` setzen (die Go-Variante
  empfiehlt das ebenfalls).
- **Connection-Pool**: Alle simulierten User teilen sich einen Pool mit bis zu
  `--max-connections` Verbindungen. Ein echter Browser hätte eigene Verbindungen. Die
  gemessene Latenz enthält auch die Wartezeit auf eine freie Verbindung.
- **Lastgenerator nicht zum Flaschenhals machen**: Bei sehr vielen Usern das Tool auf einem
  starken Rechner nahe am Server laufen lassen und die CPU-Last des Tools im Blick behalten.
  Python/asyncio schafft je nach Rechner einige tausend Requests pro Sekunde.
- **Token-Ablauf**: Access-Tokens laufen nach 10 min ab. Das Tool erneuert sie vor dem Start
  jeder Runde (Schritt `refresh_tokens`) und bei Bedarf automatisch.
- **Nie gegen eine Produktivinstanz während einer echten Veranstaltung laufen lassen.** Das
  Tool schreibt in die Datenbank (User, Anträge, Wahlen) und erzeugt bewusst Last.

## Entwicklung / Tests

`tests/fake_openslides.py` ist ein In-Memory-Fake der OpenSlides-HTTP-API für beide
Vote-APIs. Die End-to-End-Tests führen damit das komplette Szenario aus, inklusive
Parallelität, Runden, Validierung und Aufräumen:

```bash
uv pip install -e '.[test]' && .venv/bin/python -m pytest
```

Der Fake bildet die echten Schnittstellen nur nach. Vor dem ersten großen Lauf sollte das
Tool deshalb einmal mit wenigen Usern gegen eine Testinstanz laufen.
