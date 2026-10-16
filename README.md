# os-perf – Last- und Performancetest für OpenSlides-Abstimmungen

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

Danach steht `os-perf` (bzw. `.venv/bin/os-perf`) zur Verfügung.

## Benutzung

Erst prüfen, ob alles passt. Dabei wird nichts angelegt:

```bash
os-perf check -d openslides.example.org -u superadmin -m 1
```

Wahl mit 500 Usern:

```bash
os-perf run -d openslides.example.org -u superadmin -m 1 -n 500
```

Drei Wahlen parallel:

```bash
os-perf run -d openslides.example.org -m 1 -n 500 -P 3 -t assignment
```

Antragsabstimmung statt Wahl:

```bash
os-perf run -d openslides.example.org -m 1 -n 500 -t motion
```

Gemischt, zwei Wahlen und zwei Anträge parallel:

```bash
os-perf run -d openslides.example.org -m 1 -n 500 -P 4 -t assignment,motion
```

Vollautomatisch (z. B. für CI), 3 Runden ohne Rückfrage:

```bash
os-perf run -d openslides.example.org -m 1 -n 200 -P 2 --no-wait --rounds 3
```

Realistischer: Stimmen über 30 s verteilen, höchstens 200 gleichzeitig:

```bash
os-perf run -d openslides.example.org -m 1 -n 1000 --vote-spread 30 --vote-concurrency 200
```

Lokale Dev-Instanz:

```bash
os-perf run -d localhost:8000 -m 1 -n 50
```

Wenn `-p` fehlt, liest das Tool das Passwort aus der Umgebungsvariable `OS_PERF_PASSWORD`
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
| `--keep` | nichts löschen (später `os-perf cleanup`) | aus |
| `--config` | JSON-Datei mit Defaults, siehe `config.example.json` | `./config.json` |

Die vollständige Liste zeigt `os-perf run --help`.
