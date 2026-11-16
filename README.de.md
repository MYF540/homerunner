# Homerunner

[English](README.md) | [Deutsch](README.de.md)

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Eigene Home-Assistant-Integration, die einen selbst betriebenen GitHub-Actions-Runner-Host bei Bedarf ein- und ausschaltet und geplante Workflows pünktlich startet.

- **Bei Leerlauf ausschalten.** Nach einer einstellbaren Leerlauffrist ohne wartende oder laufende Jobs wird der Host über ein Wächter-Skript auf dem Host heruntergefahren.
- **Bei Bedarf wecken.** Steht in irgendeinem Repository einer Organisation oder eines Benutzers ein Job für Ihre Runner-Labels in der Warteschlange, schaltet Home Assistant den Host über sein BMC (Redfish) ein, optional zusätzlich per Wake-on-LAN. Der Job wartet bei GitHub, bis die Runner wieder da sind.
- **Geplanter Dispatch.** Workflows werden zu festen Zeiten ausgelöst (zum Beispiel eine nächtliche Testsuite um 03:00 Uhr), der Host wird vorher geweckt. Verpasste Termine werden nicht nachgeholt.

## So funktioniert es

Home Assistant fragt die GitHub-API alle 60 Sekunden nach wartenden und laufenden Jobs in allen nicht archivierten Repositories des Eigentümers ab (abzüglich der ausgeschlossenen). Ein Job zählt nur, wenn alle seine Labels in Ihren konfigurierten Runner-Labels enthalten sind. Das BMC wird alle 30 Sekunden nach Stromzustand und Leistungsaufnahme gefragt, das Wächter-Skript auf dem Host per SSH nach dem Runner-Status.

Ein Zustandsautomat (`off`, `starting`, `ready`, `active`, `shutting_down`, `wake_failed`) entscheidet:

- **Wecken**, wenn die Automatik an ist und ein passender Job wartet, das Vorlauf-Fenster eines Zeitplans begonnen hat oder der Host so lange aus war wie die Pflicht-Weck-Frist (Standard 5 Tage, damit die Runner-Registrierungen erhalten bleiben).
- **Herunterfahren**, wenn für die Leerlauffrist kein passender Job wartet oder läuft (Standard 2 Stunden nach echter Arbeit, 20 Minuten, wenn seit dem Wecken kein Job kam).
- **Nie herunterfahren**, solange die Automatik oder das **Automatische Herunterfahren** aus ist, eine Wach-halten-Frist läuft, ein Zeitplan in weniger als 30 Minuten fällig ist, die Mindestlaufzeit (15 Minuten) nicht erreicht ist, GitHub nicht erreichbar ist oder der Stromzustand unbekannt ist.
- **Startgrenze:** höchstens 8 Starts pro Tag (Ortszeit). Danach bleibt der Host bis Mitternacht an, und Sie werden benachrichtigt.

### Sicherheitsmodell

Home Assistant schaltet den Host nie von sich aus über das BMC aus. Automatisches und manuelles Herunterfahren laufen über `homerunner-guard` auf dem Host (siehe [server/install.md](server/install.md)):

- Aufgerufen wird es per SSH mit einem Schlüssel, der in `authorized_keys` fest an diesen einen Befehl gebunden ist. Das Skript akzeptiert nur `status`, `shutdown` und `cancel`.
- Bei `shutdown` lehnt es ab, wenn ein Runner gerade einen Job ausführt, die Sperrdatei existiert (Wartungsschalter) oder Benutzer angemeldet sind. Wurden schon Runner gestoppt und es taucht dann ein Job auf, startet es sie wieder.
- Nur wenn alle Prüfungen bestehen, stoppt es die Runner und führt den Poweroff-Befehl aus.
- Am Wächter vorbei geht nur der ausdrückliche Knopf „Force off“ oder `homerunner.shutdown` mit `force: true`, die über das BMC ausschalten. Der Knopf ist standardmäßig deaktiviert.
- Scheitert SSH, wird nichts automatisch heruntergefahren.

## Voraussetzungen

| Was | Einzelheiten |
|---|---|
| Home Assistant | 2025.3.0 oder neuer (siehe `hacs.json`) |
| BMC | Redfish über HTTPS, von Home Assistant erreichbar. Eigener BMC-Benutzer mit der kleinsten Rolle, die ein- und ausschalten darf. |
| Runner-Host | SSH von Home Assistant, `homerunner-guard` installiert (Python 3, nur Standardbibliothek) |
| Wake-on-LAN (optional) | MAC-Adresse des Hosts; Home Assistant muss im selben Layer-2-Netz sein |
| GitHub, Beobachter-Token | Fine-grained Personal Access Token, Eigentümer die Organisation (oder Ihr Benutzer), **alle Repositories**, Berechtigung **Actions: Read** |
| GitHub, Dispatch-Token | Nur für Zeitpläne: ein Fine-grained Token je Zeitplan, beschränkt auf das Ziel-Repository, **Actions: Read and write**. Bleibt das Feld leer, wird das Beobachter-Token verwendet, das dann ebenfalls Schreibrecht braucht. |

Tokens werden nur im Config-Entry gespeichert, nicht geloggt und nicht in Attributen angezeigt.

## Installation

### HACS (benutzerdefiniertes Repository)

1. In HACS das Menü öffnen, **Benutzerdefinierte Repositories** wählen und `https://github.com/MYF540/homerunner` mit dem Typ **Integration** hinzufügen.
2. **Homerunner** herunterladen und Home Assistant neu starten.

### Manuell

`custom_components/homerunner` in das Verzeichnis `custom_components` Ihrer Home-Assistant-Konfiguration kopieren und neu starten.

## Server einrichten

Einzelheiten stehen in [server/install.md](server/install.md) (englisch), alle Optionen des Wächters in [server/guard.conf.example](server/guard.conf.example). Kurz:

1. `server/homerunner-guard` als `/usr/local/bin/homerunner-guard` installieren, `/etc/homerunner/guard.conf` (alle Schlüssel optional) und `/var/lib/homerunner` anlegen.
2. Eigenen Benutzer anlegen (für den Standard `mode = lxd` in der Gruppe `lxd`; `mode = host` setzen, wenn die Runner-Dienste direkt auf dem Host laufen).
3. Schlüsselpaar erzeugen und den öffentlichen Schlüssel in `authorized_keys` mit `command="/usr/local/bin/homerunner-guard",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty` eintragen. Der private Schlüssel darf keine Passphrase haben.
4. sudoers-Regel für `systemctl poweroff` anlegen (im Host-Modus zusätzlich zum Stoppen und Starten der Runner-Dienste).
5. Optional: Wartungsschalter `/etc/homerunner/inhibit`, Hooks vor dem Herunterfahren, Marker „geplant aus“ für das Monitoring.
6. BMC-Benutzer anlegen; in der Firmware „Verhalten nach Stromausfall“ auf „letzter Zustand“ stellen.
7. Testen mit `homerunner-guard status` und, mit `dry_run = true` in `guard.conf`, mit `shutdown` und `cancel`.

## Einrichtung in Home Assistant

Die Integration unter **Einstellungen > Geräte & Dienste > Integration hinzufügen > Homerunner** hinzufügen.

1. **GitHub-Zugang:** Token, API-Basis-URL (nur für GitHub Enterprise Server ändern), Organisation oder Benutzer, ausgeschlossene Repositories (Namen ohne Eigentümer), Runner-Labels (Standard `self-hosted`). Das Token wird geprüft, indem Repositories aufgelistet und Läufe gelesen werden.
2. **Runner-Server:** BMC-Adresse, Benutzer, Passwort, „BMC-Zertifikat prüfen“ (standardmäßig aus, weil BMCs meist selbstsignierte Zertifikate haben), optional MAC-Adresse für Wake-on-LAN, SSH-Host, Port, Benutzer und privater Schlüssel, optional SSH-Host-Key. Bleibt der Host-Key leer, wird er abgerufen und auf der nächsten Seite von Ihnen bestätigt; danach ist er fest hinterlegt. BMC und Wächter werden geprüft, bevor der Eintrag angelegt wird.
3. **Regeln und Filter** (Optionen des Eintrags): Leerlauffrist, kurze Frist, Mindestlaufzeit, Starts pro Tag, Pflicht-Wecken nach N Tagen und seine Dauer, Weck-Zeitlimit, kein Herunterfahren vor einem Zeitplan, Warnung bei wartenden Jobs, ausgeschlossene Repositories, Runner-Labels, Webhook.
4. **Zeitpläne** werden als Untereinträge der Integration angelegt (siehe unten).

Ein neuer Eintrag startet mit **Automatik aus**, **Automatisches Herunterfahren aus** und **Trockenlauf an**. Bestehende Einträge ohne diese Einstellung gelten als aus.

### Optionen

| Option | Standard | Bedeutung |
|---|---|---|
| Leerlauffrist nach Arbeit | 2 h | Zeit ohne passende Jobs bis zum Herunterfahren, wenn seit dem Wecken mindestens ein Job lief. Auch als Number-Entität (0,25 bis 24 h). |
| Kurze Frist | 20 min | Gilt stattdessen, wenn seit dem Wecken kein Job kam. Auch als Number-Entität (5 bis 240 min). |
| Mindestlaufzeit | 15 min | Vorher kein Herunterfahren. |
| Maximale Starts pro Tag | 8 | Schutz vor ständigem Ein- und Ausschalten. |
| Pflicht-Wecken nach | 5 Tagen | War der Host so lange aus, wird er einmal gestartet, damit die Runner-Registrierungen erhalten bleiben (GitHub entfernt Runner, die 14 Tage offline sind). |
| Dauer des Pflicht-Weckens | 30 min | So lange bleibt er an, danach gilt die kurze Frist. |
| Weck-Zeitlimit | 10 min | Ist bis dahin kein Runner bereit, wechselt der Zustand auf `wake_failed`, Sie werden benachrichtigt, und das Wecken wird je Zeitlimit einmal wiederholt. |
| Kein Herunterfahren vor einem Zeitplan | 30 min | |
| Warnung bei wartenden Jobs | 15 min | Benachrichtigung, wenn ein passender Job wartet, obwohl der Host läuft. |

## Empfohlene Einführung

1. **Trockenlauf und Automatik an, ohne echtes Schalten.** **Trockenlauf** an lassen und **Automatik** einschalten. Der Zustandsautomat entscheidet und protokolliert, Benachrichtigungen tragen den Präfix „[dry run]“, aber nichts wird ein- oder ausgeschaltet. Einige Tage die Entitäten *Zustand*, *Herunterfahren um* und *Letzter Weckgrund* sowie das Logbuch beobachten.
2. **Leerlauffrist einstellen** mit der Number-Entität *Idle time*. Ein höherer Wert hält den Host zwischen Pull Requests an, ein niedrigerer spart mehr Strom, führt aber zu mehr Starts.
3. **Wecken aktivieren, von Hand herunterfahren:** **Trockenlauf** ausschalten, **Automatisches Herunterfahren** aus lassen. Der Host wird jetzt echt (BMC) geweckt, wenn Jobs warten, fährt aber nie von selbst herunter: Leerlauffrist und *Herunterfahren um* werden weiter angezeigt, der Blocker `auto_shutdown_off` steht in `blockers`. Von Hand herunterfahren mit *Safe shutdown* (Taste oder Dienst), das weiter funktioniert. Den Wächter solange auf `dry_run = true` lassen oder die Sperrdatei `/etc/homerunner/inhibit` angelegt halten (siehe [server/install.md](server/install.md)).
4. **Automatisches Herunterfahren aktivieren:** Sperrdatei entfernen, den Wächter auf `dry_run = false` setzen und **Automatisches Herunterfahren** einschalten. Das erste echte Herunterfahren und Wecken von Hand begleiten.

Der Trockenlauf ist ein gemeinsamer Schalter für beide Richtungen: Er betrifft Wecken und Herunterfahren. Im Trockenlauf geht der Zustandsautomat davon aus, dass seine Aktionen gelungen sind. **Automatisches Herunterfahren** steuert nur, ob die Leerlaufregeln ein Herunterfahren auslösen dürfen.

## Entitäten

Alle Entitäten gehören zu einem Gerät je Eintrag. „Diagnose“ und „Konfiguration“ bezeichnen die Entitätskategorie. Die Namen sind in der deutschen Oberfläche übersetzt; hier stehen die englischen Namen.

### Server

| Entität | Typ | Beschreibung |
|---|---|---|
| State | Sensor | `off`, `starting`, `ready`, `active`, `shutting_down`, `wake_failed`, `unknown`. Attribute: `blockers`, `dry_run`. |
| Queued jobs | Sensor | Wartende passende Jobs; Attribut `jobs` mit Repository, Name und URL. |
| Running jobs | Sensor | Laufende passende Jobs; gleiche Attribute. |
| Shutdown at | Sensor | Zeitpunkt, an dem die Leerlauffrist abläuft. |
| Power | Sensor | Watt laut BMC; nicht verfügbar, wenn das BMC nicht lesbar ist. |
| Last wake reason | Sensor (Diagnose) | `queued_job`, `schedule`, `manual`, `forced_wake`. |
| Starts today | Sensor (Diagnose) | Zähler für die Startgrenze. |
| API rate limit remaining | Sensor (Diagnose) | Attribute `limit`, `reset_at`. |
| Server on | Binärsensor | Stromzustand laut BMC. |
| GitHub reachable | Binärsensor (Diagnose) | |
| Problem | Binärsensor (Diagnose) | An bei Weckfehler, wenn GitHub oder das BMC nicht lesbar sind oder der Host läuft, der Wächter aber nicht antwortet. |
| Automatic | Schalter (Konfiguration) | Hauptschalter für automatisches Wecken und Herunterfahren. |
| Automatic shutdown | Schalter (Konfiguration) | Leerlaufregeln dürfen herunterfahren. Aus: nur Wecken; manuelles sicheres Herunterfahren geht weiter. |
| Dry run | Schalter (Konfiguration) | Entscheiden und protokollieren, aber nichts schalten. |
| Idle time, Short idle time | Number (Konfiguration) | Leerlauffristen im Betrieb ändern. |
| Wake | Knopf | Jetzt einschalten. |
| Safe shutdown | Knopf | Über den Wächter herunterfahren (wird abgelehnt, wenn er es so entscheidet). |
| Keep awake for 2 hours | Knopf | Sperrt das automatische Herunterfahren für 2 Stunden. |
| Force off | Knopf | Ausschalten über das BMC ohne Wächter. Standardmäßig deaktiviert; laufende Jobs gehen verloren. |

### Je Zeitplan

| Entität | Typ | Beschreibung |
|---|---|---|
| Enabled | Schalter (Konfiguration) | |
| Dispatch now | Knopf | Löst den Dispatch (mit Wiederholungen) sofort aus. |
| Next dispatch | Sensor | Nächster Auslösezeitpunkt. |
| Last dispatch | Sensor | Zeitpunkt des letzten Dispatch; Attribute `result`, `status`, `run_id`, `error`, `attempts`. |
| Last run | Sensor | Ergebnis oder Status des Workflow-Laufs; Attribute `url`, `run_id`. |

## Dienste

Das optionale Feld `config_entry` wählt den Eintrag, wenn mehrere eingerichtet sind.

| Dienst | Felder | Beschreibung |
|---|---|---|
| `homerunner.wake` | | Einschalten, unabhängig vom Schalter Automatic. |
| `homerunner.shutdown` | `force` (Standard false) | Über den Wächter herunterfahren. Mit `force: true` schaltet das BMC aus, ohne den Wächter zu fragen. |
| `homerunner.keep_awake` | `duration` (Standard 2 Stunden) | Automatisches Herunterfahren so lange sperren. |
| `homerunner.dispatch` | `schedule` oder `repository` + `workflow`; optional `ref`, `inputs` | Workflow eines Zeitplans oder beliebigen Workflow eines beliebigen Repositories auslösen. `ref` und `inputs` überschreiben die Werte des Zeitplans. `inputs` muss ein JSON-Objekt sein. |

```yaml
service: homerunner.dispatch
data:
  repository: example-org/example-repo
  workflow: nightly.yml
  ref: main
  inputs:
    full: "true"
```

## Ereignisse

Für Automationen. Ereignisse des Zustandsautomaten:

| Ereignis | Nutzdaten | Wann |
|---|---|---|
| `homerunner_woke` | `wake_reason`, `seconds`, `recovered` | Der Host ist oben und die Runner sind bereit. |
| `homerunner_shut_down` | | Der Host wurde nach einem Herunterfahren als ausgeschaltet erkannt. |
| `homerunner_shutdown_refused` | `reason` (`job_running`, `inhibited`, `users_logged_in`, `error`) | Der Wächter hat abgelehnt oder ist gescheitert. |
| `homerunner_wake_failed` | `entry_id`, `error`, `minutes` | Der Host wurde nicht rechtzeitig bereit. Zusätzlich eine dauerhafte Benachrichtigung. |
| `homerunner_start_limit` | `entry_id`, `starts_today`, `max_starts_per_day` | Startgrenze erreicht. Zusätzlich eine dauerhafte Benachrichtigung. |
| `homerunner_unexpected_off` | `entry_id`, `previous_state` | Der Host ging ohne Herunterfahr-Anforderung aus. Zusätzlich eine dauerhafte Benachrichtigung. |
| `homerunner_queue_stuck` | `entry_id`, `queued`, `waiting_since` | Ein Job wartet länger als die Warnfrist, obwohl der Host läuft. Zusätzlich eine dauerhafte Benachrichtigung. |
| `homerunner_shutdown_stuck` | `entry_id`, `minutes`, `accepted` | Host 15 Minuten nach einer Herunterfahr-Anforderung noch an. Zusätzlich eine dauerhafte Benachrichtigung. |

Ereignisse der Zeitpläne:

| Ereignis | Nutzdaten |
|---|---|
| `homerunner_dispatched` | `entry_id`, `schedule`, `repository`, `workflow`, `ref`, `run_id`, `status`, `attempts` |
| `homerunner_dispatch_failed` | dasselbe, zusätzlich `error` |

Ein abgelehntes Token (HTTP 401) erzeugt außerdem einen Reparatur-Hinweis „GitHub token rejected“. Das Beobachter-Token lässt sich über den Re-Authentifizierungs-Ablauf erneuern.

## Zeitpläne

Zeitplan über die Integrationsseite hinzufügen: **Zeitplan hinzufügen** (Untereintrag vom Typ „schedule“). Vorhandene Zeitpläne lassen sich dort neu konfigurieren.

| Feld | Standard | Hinweis |
|---|---|---|
| Name | | |
| Repository | | `example-org/example-repo` |
| Workflow | | Dateiname oder ID, zum Beispiel `nightly.yml` |
| Branch oder Tag (`ref`) | `main` | |
| Eingaben | `{}` | JSON-Objekt |
| Uhrzeit | 03:00 | |
| Wochentage | alle | |
| Zeitzone | Zeitzone von HA | IANA-Name; Sommerzeit wird beachtet. Nicht existierende Ortszeiten feuern zum ersten gültigen Zeitpunkt, doppelte nur einmal. |
| Vorher wecken | an | |
| Vorlauf | 15 min | Mindestens gemessene Bootzeit plus Reserve wählen. |
| Wiederholungen | 5 | Abstände 30 s, 60 s, 120 s, 240 s, 480 s. |
| Aktiv | an | |
| Dispatch-Token | leer | Optional; sonst das Beobachter-Token. |

Verhalten:

- **Kein Nachholen.** War Home Assistant zum Auslösezeitpunkt aus, wird der Dispatch nicht später nachgeholt; der nächste Termin wird ab „jetzt“ berechnet.
- Wiederholt wird nur bei Netzfehlern, Zeitüberschreitungen, 5xx-Antworten und kurzen Rate-Limits. 401 erzeugt den Reparatur-Hinweis; 403, 404 und 422 schlagen sofort fehl.
- Der Dispatch wartet nicht darauf, dass der Host bereit ist. Scheitert das Wecken, wartet der Job bei GitHub.
- Liefert GitHub eine Lauf-ID, verfolgt „Last run“ diesen Lauf bis zum Ende.

## Optionaler Webhook

Das Polling ist immer aktiv. Der Webhook löst nur eine sofortige Aktualisierung der Warteschlange aus, wenn GitHub einen passenden `workflow_job` meldet, sodass der Host ohne Warten auf die nächste Abfrage geweckt wird.

1. In den Optionen der Integration den **workflow_job-Webhook** aktivieren. ID und Secret werden erzeugt; die nächste Seite zeigt Payload-URL und Secret.
2. In GitHub (Organisationseinstellungen > Webhooks > Add webhook):
   - Payload URL: die von Home Assistant angezeigte URL. Sie muss aus dem Internet erreichbar sein (zum Beispiel Nabu Casa Cloud oder ein eigener Tunnel).
   - Content type: `application/json`
   - Secret: das von Home Assistant angezeigte Secret
   - Events: einzelne Ereignisse wählen, **Workflow jobs**
3. Anfragen mit ungültigem `X-Hub-Signature-256` werden mit 401 abgewiesen; `ping` wird mit 200 beantwortet.

## Energie-Dashboard

Der Sensor *Power* liefert Watt. Für das Energie-Dashboard unter **Einstellungen > Geräte & Dienste > Helfer > Integral-Sensor** einen Helfer anlegen (Riemann-Summe, Präfix k, Zeiteinheit Stunden) mit dem Power-Sensor als Quelle und das Ergebnis als Verbrauchsquelle hinzufügen. Bei ausgeschaltetem Host meldet das BMC meist weiterhin die Standby-Leistung.

## Fehlersuche

| Symptom | Prüfen |
|---|---|
| Einrichtung: `cannot_connect_bmc` / `invalid_auth_bmc` | BMC-Adresse, Benutzer und Passwort; Redfish aktiv; Zertifikatsprüfung (bei selbstsignierten Zertifikaten aus). |
| `cannot_connect_guard` | SSH-Host, Port, Benutzer und Schlüssel; `homerunner-guard` installiert und ausführbar; Zeile in `authorized_keys`; Schlüssel ohne Passphrase. |
| `guard_protocol_error` | Der feste Befehl in `authorized_keys` ist nicht das Wächter-Skript. |
| Host wird nicht geweckt | Automatic an und Dry run aus? Attribut `blockers` des Sensors *State*, *Starts today* gegen die Grenze, *GitHub reachable* an? Passen alle Labels des Jobs in Ihre Runner-Labels? Ausgeschlossene und archivierte Repositories werden ignoriert. |
| Host fährt nicht herunter | Dieselben Prüfungen, dann die Leerlauffrist (*Shutdown at*), ein bald fälliger Zeitplan, die Mindestlaufzeit, Ereignisse `homerunner_shutdown_refused`, die Sperrdatei, angemeldete Benutzer (auch per SSH) oder `dry_run` in `guard.conf`. |
| `wake_failed` | Der Host meldete innerhalb des Weck-Zeitlimits keine bereiten Runner. BMC und Bootzeit prüfen, ob die Runner-Dienste beim Booten starten und ob `status` des Wächters aktive Runner zeigt. |
| Token abgelehnt | Über den Reparatur-Hinweis oder die Re-Authentifizierung erneuern; Berechtigung „Actions“ und Repository-Zugriff prüfen. |
| Rate-Limit | Siehe *API rate limit remaining*. Das Limit liegt bei 5000 Anfragen pro Stunde je GitHub-Benutzer und wird mit jeder anderen Nutzung dieses Benutzers geteilt. Unveränderte Antworten (ETag) zählen nicht. Fällt das Restkontingent unter 10 Prozent, werden Repositories ohne neue Pushes übersprungen. |
| Webhook wirkt nicht | Payload-URL erreichbar, Secret gleich, Ereignis „Workflow jobs“, Recent Deliveries in GitHub ansehen. |

Diagnose-Daten lassen sich auf der Integrationsseite herunterladen; Geheimnisse sind geschwärzt. Debug-Protokoll mit `logger: logs: custom_components.homerunner: debug`.

## Einschränkungen

- Home Assistant wird zur Voraussetzung dafür, dass CI läuft. Ist es aus, während der Host aus ist, weckt ihn niemand; dann das BMC von Hand bedienen.
- GitHub hält einen wartenden Job bis zu 24 Stunden. Braucht der Host länger zum Start, lässt GitHub den Job fehlschlagen.
- Das API-Limit von 5000 Anfragen pro Stunde gilt je GitHub-Benutzer, nicht je Token.
- Bis zum Wecken vergehen bis zu 60 Sekunden Polling-Verzug (weniger mit Webhook) plus die Bootzeit des Hosts.
- Zeitpläne werden nach einem Ausfall nicht nachgeholt.
- Herunterfahren heißt ausschalten (S5), nicht Standby.
- Pro Config-Entry ein Wächter-Host; ein Eintrag beobachtet einen Eigentümer.
- Jobs werden nur über Labels zugeordnet; die Runner-Liste von GitHub liest die Integration nicht.

## Lizenz

[MIT](LICENSE)
