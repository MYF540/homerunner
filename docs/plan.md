# Homerunner – Plan für die Home-Assistant-Integration

Stand: 16.11.2026. Status: **Plan, noch nichts umgesetzt.**
Grundlagen: Unterlagen zum Runner-Server (nicht öffentlich, nicht in diesem Repository).

---

## 1. Ziel und Kurzfassung

Der Runner-Server (Zwei-Sockel-Server mit mehreren LXD-Runnern für das Anwendungs-Repository der Organisation) läuft heute rund um die Uhr.
Homerunner soll Strom sparen und nebenbei den Nachtlauf pünktlich starten:

1. **Abschalten:** Nach X Stunden ohne wartende oder laufende Jobs fährt der Server sauber herunter.
2. **Wecken:** Sobald ein Job für die eigenen Runner in der Warteschlange steht, schaltet Home Assistant den Server
   ein. Der Job wartet solange bei GitHub.
3. **Planer:** Zu festen Zeiten löst Home Assistant ein `workflow_dispatch` aus (erster Fall: Nacht-Suite 03:00 Uhr
   Europe/Berlin) und weckt den Server vorher.

Die wichtigsten Entscheidungen, jeweils unten begründet:

| Frage | Empfehlung |
|---|---|
| Plugin oder App? | **Custom Integration** (`custom_components/homerunner`), über HACS installierbar. Keine App (früher Add-on). |
| Herunterfahren oder Schlafen? | **Herunterfahren (S5).** Suspend ist auf dieser Hardware keine Option. |
| Wecken per WoL oder BMC (z. B. iBMC, iDRAC, iLO)? | **iBMC über Redfish.** WoL nur als optionaler zweiter Weg. |
| Jobs erkennen per Webhook oder Polling? | **Polling** der GitHub-API als Grundlage. Webhook später als optionaler Beschleuniger. |
| Wie sicher herunterfahren? | Über ein kleines **Wächter-Skript auf dem Server**, das lokal prüft, ob wirklich kein Job läuft. |

---

## 2. Entscheidungen im Einzelnen

### 2.1 Integration statt App

Home Assistant nennt Add-ons seit 2026.2 „Apps“. Eine App ist ein eigener Container neben Home Assistant; sie läuft
nur auf HA OS bzw. Supervised und müsste ihre Zustände erst über MQTT oder die HA-API hineinreichen. Homerunner
braucht aber genau das, was eine Integration von Haus aus hat: Einrichtung über die Oberfläche (Config-Flow),
Entitäten, Dienste, Zeitsteuerung in der Zeitzone der Instanz, Reparatur-Hinweise. Deshalb eine Custom Integration.
Sie läuft auf jeder Installationsart und lässt sich über HACS als „Custom Repository“ einbinden.

### 2.2 Herunterfahren statt Schlafen

- **Suspend-to-RAM (S3):** Zwei-Sockel-Server dieser Klasse bieten S3 in der Regel gar nicht an. Selbst wenn, wären
  Aufwachen mit sehr viel ECC-RAM, mehreren Controllern und verschachteltem Docker ein Risiko ohne Gegenwert.
- **Hibernate (S4):** bräuchte Swap in RAM-Größe (vorhanden ist nur wenig) und schriebe bei jedem Zyklus viel auf die
  SSD, die ohnehin stark belastet ist.
- **Herunterfahren (S5):** ist der Zustand, für den der Server gebaut ist. Laut den Server-Unterlagen starten Container,
  Runner, Docker und Dashboard nach dem Booten von selbst. Die Runner-Verbindung zu GitHub reißt in jedem Fall ab,
  ein Schlafzustand hätte also auch hier keinen Vorteil.

Preis des Herunterfahrens ist die Startzeit (POST eines Servers dieser Klasse dauert erfahrungsgemäß einige Minuten, dazu Ubuntu
und Container). In Phase 0 wird sie gemessen; geschätzt 4 bis 6 Minuten, die ein Job zusätzlich wartet.

Im ausgeschalteten Zustand bleiben iBMC und Netzteile aktiv (geschätzt 10 bis 20 W). Die Ersparnis ist also
„Leerlauf minus Standby“. Beides liest die Integration aus dem iBMC aus, damit die Ersparnis gemessen statt geschätzt
wird (Abschnitt 9).

### 2.3 Wecken über iBMC statt Wake-on-LAN

| | iBMC (Redfish / IPMI) | Wake-on-LAN |
|---|---|---|
| Einschalten | ja, mit Rückmeldung | ja, ohne Rückmeldung („Paket geschickt“) |
| Zustand lesen (an/aus) | ja | nein (nur Ping) |
| Sauber ausschalten | ja (ACPI-Soft-Off) | nein |
| Leistungsaufnahme lesen | ja | nein |
| Voraussetzung | iBMC im LAN erreichbar, eigener Benutzer | WoL im BIOS und am NIC aktiv, HA im selben Layer-2-Netz |
| Unsicherheit | keine (siehe unten) | ob die Onboard-NIC des Servers WoL aus S5 kann, ist unbelegt |

Entschieden: iBMC als Hauptweg über **Redfish** (HTTPS, läuft mit dem ohnehin vorhandenen `aiohttp`, keine
Zusatzbibliothek). Bestätigt ist (16.11.2026), dass der iBMC Redfish unterstützt und Home Assistant im
selben Netz wie der iBMC liegt. Eine IPMI-Umsetzung entfällt damit. WoL bleibt eine optionale Rückfallebene: Feld
„MAC-Adresse“ im Config-Flow, Versand über die eingebaute HA-Komponente `wake_on_lan`.

### 2.4 Jobs erkennen: Polling zuerst, Webhook optional

Die Runner sind in der Organisation registriert. Beobachtet werden **alle Repositories der Organisation**, nicht
nur das Anwendungs-Repository (entschieden am 16.11.2026). GitHub hat keinen Endpunkt „alle wartenden Jobs der Organisation“. Es gibt
zwei Wege:

**Polling (Empfehlung als Grundlage).** Die Integration holt stündlich die Liste der Repositories
(`GET /orgs/{org}/repos`, archivierte ausgenommen; im Config-Flow lassen sich einzelne ausschließen) und fragt je
Repository:

```
GET /repos/{owner}/{repo}/actions/runs?status=queued
GET /repos/{owner}/{repo}/actions/runs?status=in_progress
GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs        (für jeden aktiven Lauf)
```

- Beide Statuswerte sind nötig: Ein Lauf kann `in_progress` sein, während sein nächster Job auf einen eigenen
  Runner wartet (erster Job lief auf einem GitHub-Runner).
- Entscheidend sind die **Jobs**, nicht die Läufe. Jeder Job trägt seine `runs-on`-Labels im Feld `labels`. Ein
  Job zählt für Homerunner, wenn alle seine Labels in der konfigurierbaren Runner-Label-Menge liegen (Groß-/Kleinschreibung egal). So wecken
  Läufe auf GitHub-eigenen Runnern den Server nicht.
- Jobs hinter `needs` tauchen erst auf, wenn sie an der Reihe sind. Polling fängt das von selbst ab.
- Takt: 60 Sekunden. Mit bedingten Anfragen (`If-None-Match` / ETag) zählen unveränderte Antworten nicht gegen
  das Limit, deshalb bleibt der Verbrauch auch bei vielen Repositories klein: Gezählt wird nur, wo sich etwas
  ändert. Repositories ohne Push in den letzten 30 Tagen werden nur alle 10 Minuten gefragt. Die Integration zeigt
  das verbleibende Limit als Diagnose-Sensor und streckt den Takt von selbst, wenn es knapp wird. Achtung: Das
  Limit (5.000 je Stunde) gilt je GitHub-Benutzer, nicht je Token, wird also mit anderen Token desselben Benutzers (etwa denen der Dashboard-Skripte auf dem Server) geteilt.
- Braucht nur ausgehendes HTTPS und dasselbe Token wie der Planer. Home Assistant muss nicht aus dem Internet
  erreichbar sein.
- Auch der GitHub-eigene Ersatztakt der Nacht-Suite wird so erkannt: Er legt einen Lauf an, der Job wartet, der
  Server wird geweckt.

**Webhook `workflow_job` (später, optional).** GitHub schickt bei `queued`, `in_progress` und `completed` sofort
ein Ereignis mit den Labels des Jobs, auf Organisationsebene für alle Repositories auf einmal. Dafür muss Home
Assistant aus dem Internet erreichbar sein (Nabu-Casa-Cloudhook oder eigener Tunnel), jemand mit Org-Rechten muss
den Webhook anlegen, und die Signatur (`X-Hub-Signature-256`) muss geprüft werden. Gewinn: Wecken ohne die bis zu
60 Sekunden Polling-Verzug, und die Zahl der Repositories spielt keine Rolle mehr. Bei mehreren Minuten
Startzeit fällt der Verzug kaum ins Gewicht; lohnend wird der Webhook erst, wenn die Organisation sehr viele
aktive Repositories hat. Polling bleibt in jedem Fall als Sicherheitsnetz, weil Webhooks verloren gehen können.

Ein wartender Job bleibt bei GitHub bis zu 24 Stunden in der Warteschlange, auch wenn alle passenden Runner
offline sind. Die Startzeit des Servers ist also unkritisch.

### 2.5 Sicher herunterfahren: Wächter-Skript auf dem Server

Freigegeben: Das Skript darf auf den Server (entschieden am 16.11.2026).

Regel aus den Server-Unterlagen: Nie einen Runner stoppen, während er einen Job hat. Zwischen „GitHub meldet: nichts läuft“
und dem Ausschalten liegen aber Sekunden, in denen ein Runner einen neuen Job annehmen kann. Nur der Server selbst
weiß das sicher. Deshalb:

- Skript `homerunner-guard` auf dem Host mit zwei Befehlen:
  - `status`: gibt JSON aus (je Container: Runner-Dienst aktiv, Job läuft ja/nein; angemeldete Benutzer;
    Sperrdatei vorhanden).
  - `shutdown`: prüft je Container, ob ein Job läuft (Prozess `Runner.Worker`). Wenn nein, stoppt es sofort den
    Runner-Dienst, sodass GitHub diesem Runner nichts mehr zuteilt. Sind alle gestoppt, folgt
    `systemctl poweroff`. Läuft irgendwo ein Job oder liegt eine Sperrdatei (`/etc/homerunner/inhibit`, für
    Wartung von Hand), startet es bereits gestoppte Runner wieder und meldet „abgelehnt“.
- Aufruf von Home Assistant per **SSH mit fest verdrahtetem Befehl**: eigener Schlüssel, in `authorized_keys` mit
  `command="/usr/local/bin/homerunner-guard"` und ohne Weiterleitungen eingetragen. Der Schlüssel kann nur diese
  zwei Befehle. Für `poweroff` eine sudoers-Zeile mit genau diesem einen Befehl, nach dem Muster der bestehenden
  Dashboard-Freigaben.
- `status` dient zugleich als Bereitschaftsmeldung nach dem Wecken („alle Runner-Dienste laufen“). Damit
  braucht das Token kein Org-Recht „Self-hosted runners: Read“.
- Rückfallebene, wenn SSH scheitert: kein automatisches Ausschalten. Ein hartes oder weiches Ausschalten per iBMC
  ohne Wächter gibt es nur über den ausdrücklichen Knopf „Jetzt ausschalten (erzwingen)“.

Die vorhandenen Dashboard-Skripte auf dem Server lösen eine verwandte Aufgabe; beim Umsetzen prüfen, ob sich Logik
übernehmen lässt.

---

## 3. Architektur

```
                       ┌──────────────────── Home Assistant ────────────────────┐
 GitHub API  ◀─Polling─┤ GitHub-Beobachter        Planer (Zeitpläne)            │
   (runs, jobs)        │   wartende/laufende Jobs   workflow_dispatch ──────────┼──▶ GitHub API
                       │            │                    │ „bitte wecken“       │
                       │            ▼                    ▼                      │
                       │        Energieverwalter (Zustandsautomat)              │
                       │            │                    │                      │
                       └────────────┼────────────────────┼──────────────────────┘
                          Redfish (an, Zustand,        SSH (status, shutdown)
                          Watt), optional WoL             │
                                    ▼                     ▼
                                 iBMC  ───────────  Runner-Server
                                                    homerunner-guard, Runner
```

Bausteine der Integration:

| Baustein | Aufgabe |
|---|---|
| `github.py` | API-Client: Läufe, Jobs, Dispatch, ETag-Zwischenspeicher, Fehlerklassen (401, 403/Rate-Limit, 404, 422, vorübergehend) |
| `bmc.py` | Schnittstelle „Strom“: an, Zustand, Watt. Umsetzung Redfish, dazu WoL als Zusatzweg |
| `guard.py` | SSH-Aufruf des Wächter-Skripts (`asyncssh`) |
| `coordinator.py` | zwei `DataUpdateCoordinator`: GitHub-Warteschlange (60 s) und Serverzustand (30 s) |
| `power_manager.py` | Zustandsautomat aus Abschnitt 4, einzige Stelle, die ein- und ausschaltet |
| `scheduler.py` | Zeitpläne, Vorlauf-Wecken, Dispatch mit Wiederholung |
| Plattformen | `sensor`, `binary_sensor`, `switch`, `button`, `number` |
| `config_flow.py` | Einrichtung, Optionen, Zeitpläne als Unter-Einträge (Config-Subentries) |
| `repairs.py`, `diagnostics.py` | Hinweis „Token erneuern“, Diagnose-Export mit geschwärzten Geheimnissen |

Zustände, die einen HA-Neustart überleben müssen (letzte Job-Aktivität, Weckgrund, letzter Auslösezeitpunkt je
Zeitplan), liegen in einem `Store`.

---

## 4. Zustandsautomat des Energieverwalters

```
        Weckgrund                    Runner bereit              Job erkannt
 AUS ──────────────▶ STARTET ──────────────────────▶ BEREIT ◀──────────────▶ AKTIV
  ▲                     │ nach 10 min nicht bereit      │  Leerlauffrist abgelaufen
  │                     ▼                               ▼
  │               WECKFEHLER (erneut versuchen,   FÄHRT HERUNTER ── Wächter lehnt ab ──▶ AKTIV
  │                melden)                              │
  └─────────────────────────────────────────────────────┘ iBMC meldet „aus“
```

**Weckgründe:** wartender Job für eigene Runner, Zeitplan mit Vorlauf, Knopf „Wecken“, Pflicht-Wecken
(Abschnitt 6).

**Leerlauffrist.** Die Frist beginnt, wenn kein eigener Job mehr wartet oder läuft, und beginnt bei jedem neuen
Job von vorn. Zwei Werte:

- **X Stunden** nach echter Arbeit. X ist im laufenden Betrieb über die Entität „Leerlauffrist“ einstellbar
  (Schritte von 0,25 h, Startwert 2 h), damit es sich von Hand nachjustieren lässt. Tagsüber bleibt der Server so
  zwischen zwei PRs an, statt ständig zu starten.
- **Kurze Frist** (Vorschlag 20 min), wenn seit dem Wecken gar kein Job kam. Beispiel: Für den Nachtlauf geweckt,
  aber `dev` ist unverändert und der Lauf endet sofort.

**Nicht herunterfahren, wenn**

- der Schalter „Automatik“ aus ist oder „Wach halten bis …“ gesetzt ist,
- ein Zeitplan in weniger als 30 Minuten fällig ist,
- die Mindestlaufzeit seit dem Start (Vorschlag 15 min) nicht erreicht ist,
- der Wächter ablehnt (Job läuft, Sperrdatei, jemand ist angemeldet),
- GitHub nicht erreichbar ist. Ohne verlässlichen Blick auf die Warteschlange bleibt der Server an.

**Schutz vor Dauerschleifen:** höchstens N Starts je Tag (Vorschlag 8), danach bleibt der Server bis Mitternacht an
und Home Assistant meldet es. Häufige Ein-Aus-Zyklen belasten Netzteile und Platten mehr als Dauerlauf.

**Probebetrieb („Trockenlauf“):** Ein Schalter, mit dem der Automat alles entscheidet und protokolliert, aber
nichts schaltet. Damit lässt sich einige Tage beobachten, wann er geweckt und abgeschaltet hätte.

---

## 5. Planer

Allgemein gehalten, damit er auch für andere Workflows taugt. Je Zeitplan (als Unter-Eintrag in der Oberfläche):

| Feld | Standard für die Nacht-Suite |
|---|---|
| Repository | `example-org/app` |
| Workflow | `nightly.yml` |
| Branch (`ref`) | `dev` |
| Eingaben (JSON) | `{"scheduled":"true"}` |
| Uhrzeit, Wochentage | 03:00, täglich |
| Zeitzone | `Europe/Berlin` (unabhängig von der Zeitzone der HA-Instanz, Sommerzeit wird beachtet) |
| Server vorher wecken | ja (entschieden), Vorlauf = gemessene Startzeit plus Reserve (Vorschlag 10 min) |
| Wiederholungen | 5, mit wachsendem Abstand |
| Aktiv | ja |

Verhalten nach der Vorlage:

- Wiederholt wird nur bei Netzfehlern, Zeitüberschreitung und `5xx`. Bei `401` erscheint ein Reparatur-Hinweis
  „Token erneuern“, `403`, `404` und `422` werden als Fehler gemeldet und nicht wiederholt.
- **Kein Nachholen:** War Home Assistant um 03:00 Uhr aus, wird der Termin nicht nachgeholt. Dann greift der
  Ersatztakt von GitHub, und dessen Job weckt den Server über die Warteschlangen-Erkennung.
- Der Dispatch wird **nicht** davon abhängig gemacht, dass der Server schon bereit ist. Scheitert das Wecken,
  wartet der Job bei GitHub und der Fehler wird gemeldet.
- Seit Februar 2026 kann die Dispatch-API die Lauf-ID zurückgeben. Das wird genutzt, um „letzter geplanter Lauf“
  ohne Suche anzuzeigen; die Vorlage geht noch von `204` ohne ID aus, beides wird unterstützt.
- Ein zusätzlicher systemd-Timer auf dem Server wird nicht eingerichtet.

Für die Nacht-Suite wird vorab geweckt (entschieden am 16.11.2026). An Tagen ohne Änderung an `dev` läuft der Server
dann dank kurzer Frist etwa 25 Minuten und geht wieder aus. Das Feld bleibt je Zeitplan schaltbar.

---

## 6. Nebenwirkungen aus den Server-Unterlagen und wie der Plan sie behandelt

| Punkt | Behandlung |
|---|---|
| Runner nie während eines Jobs stoppen | Wächter-Skript (2.5), kein automatisches Ausschalten ohne Wächter |
| Jobs brechen beim Neustart ab | wie oben; zusätzlich Schalter „Automatik“ und Sperrdatei für Wartung |
| Warnung „Runner über 5 Minuten offline“ | Dashboard und Warnungen laufen auf dem Server selbst und schweigen, solange er aus ist. Kritisch sind die Minuten vor dem Ausschalten und nach dem Start. In Phase 0 die Warnlogik der Dashboard-Skripte prüfen und bei Bedarf eine Markierung „geplant aus“ auswerten, die der Wächter vor dem Ausschalten setzt. |
| Status-Issue veraltet im Schlaf | Wächter schreibt vor dem Ausschalten einmal „geplant aus seit …“ (über das Status-Skript des Servers), nach dem Start läuft es normal weiter |
| Metriken in der Cloud | Lücken in den Metriken sind dann gewollt. Markierung beim Aus- und Einschalten als Annotation; eigene Alarmregeln auf „keine Daten“ prüfen. |
| Warnung „Job wartet über 3 Stunden“ | kann bei ausgeschaltetem Server nicht mehr vom Dashboard kommen. Home Assistant übernimmt: Benachrichtigung, wenn ein eigener Job länger als 15 Minuten wartet, obwohl geweckt wurde. |
| Runner nach 14 Tagen offline entfernt | **Pflicht-Wecken:** War der Server N Tage aus (Vorschlag 5), wird er für 30 Minuten gestartet. Das hält die Registrierung und gibt `unattended-upgrades` Gelegenheit. |
| Unerwarteter Ausfall | Bisher meldet das Dashboard. Künftig unterscheidet Home Assistant „geplant aus“ von „aus, obwohl er an sein sollte“ und meldet Letzteres. |

Neue Abhängigkeit: Home Assistant wird zur Voraussetzung dafür, dass CI läuft. Ist HA aus, während der Server
schläft, weckt ihn niemand. Gegenmittel: Benachrichtigung auf dem Handy bei Weckfehlern, der iBMC bleibt von Hand
bedienbar, und im BIOS „nach Stromausfall: letzter Zustand“ einstellen und dokumentieren.

---

## 7. Einrichtung und Entitäten

**Config-Flow**

1. GitHub: Token, API-Basis (Standard `https://api.github.com`), Organisation oder Benutzer, ausgeschlossene
   Repositories, Runner-Labels. Prüfung ohne Lauf: Repository-Liste abrufen und für eines die Läufe lesen.
2. Server: iBMC-Adresse, Benutzer, Passwort (Redfish), optional MAC für WoL; SSH-Adresse,
   Benutzer und Schlüssel für den Wächter. Prüfung: Zustand lesen, `status` aufrufen.
3. Regeln: Leerlauffrist X, kurze Frist, Mindestlaufzeit, Starts je Tag, Pflicht-Wecken, Trockenlauf.
4. Zeitpläne als Unter-Einträge, beliebig viele.

**Token:** Weil alle Repositories beobachtet werden, aber nur einzelne Workflows ausgelöst werden, sind zwei
Fine-grained PATs der sauberste Zuschnitt:

- **Beobachter-Token:** Eigentümer die Organisation, „All repositories“, nur „Actions: Read“ (Metadata: Read kommt
  automatisch dazu). Kann nichts verändern.
- **Auslöse-Token je Zeitplan:** nur das betroffene Repository, „Actions: Read and write“. Für die Nacht-Suite
  also genau das Token aus der Vorlage.

Wer es einfacher will, trägt ein einziges Token mit „Actions: Read and write“ auf allen Repositories ein; das Feld
am Zeitplan bleibt dann leer. Tokens werden nur im Config-Entry gespeichert, nie geloggt, nie in Attributen
gezeigt. Der iBMC bekommt einen eigenen Benutzer mit der kleinsten Rolle, die Ein- und Ausschalten
erlaubt.

**Entitäten (Gerät „Runner-Server“)**

| Typ | Name | Inhalt |
|---|---|---|
| `sensor` | Zustand | aus, startet, bereit, aktiv, fährt herunter, Weckfehler |
| `sensor` | Wartende Jobs / Laufende Jobs | Anzahl eigener Jobs, Attribute mit Lauf-Links |
| `sensor` | Herunterfahren um | Zeitpunkt, an dem die Frist abläuft |
| `sensor` | Leistung | Watt laut iBMC; daraus Energie für das Energie-Dashboard |
| `sensor` | Letzter Weckgrund, Starts heute | Diagnose |
| `binary_sensor` | Server an, GitHub erreichbar, Problem | |
| `switch` | Automatik, Trockenlauf | |
| `number` | Leerlauffrist (Stunden), Kurze Frist (Minuten) | ohne Umweg über die Optionen änderbar, wirken sofort auf die laufende Frist |
| `button` | Wecken, Sicher herunterfahren, Wach halten (2 h) | |

Je Zeitplan: `switch` Aktiv, `button` Jetzt auslösen, `sensor` Nächste Auslösung, `sensor` Letztes Auslösen
(Zeit, HTTP-Status), `sensor` Letzter Lauf (Ergebnis, Link).

**Dienste:** `homerunner.wake`, `homerunner.shutdown`, `homerunner.keep_awake` (Dauer), `homerunner.dispatch`
(Zeitplan oder freie Angaben; `ref` und `inputs` überschreibbar). Dazu Ereignisse für eigene Automationen
(geweckt, heruntergefahren, Dispatch fehlgeschlagen).

---

## 8. Repository-Aufbau

```
custom_components/homerunner/     Integration (Abschnitt 3), translations/en.json und de.json
server/homerunner-guard           Wächter-Skript für den Host
server/install.md                 SSH-Schlüssel, sudoers, Sperrdatei, iBMC-Benutzer
tests/                            pytest mit pytest-homeassistant-custom-component, GitHub und iBMC als Attrappen
docs/plan.md                      dieses Dokument
hacs.json, README.md (Englisch), LICENSE (MIT, Vorschlag)
.github/workflows/                hassfest, HACS-Prüfung, Tests (auf GitHub-Runnern, nicht auf dem eigenen Server)
```

### Öffentliches Repository

Das Repository soll öffentlich über HACS nutzbar sein (entschieden am 16.11.2026). Daraus folgt:

- Nichts im Code ist auf ein bestimmtes Repository oder Servermodell festgelegt. Repository, Workflow, Labels und Fristen sind
  reine Konfiguration; die Nacht-Suite steht nur als Beispiel in der Dokumentation.
- Redfish ist herstellerübergreifend. Die Integration nutzt nur Standardpfade (`Systems`, `Chassis/…/Power`) und
  findet sie über den Dienst-Einstieg, statt herstellerspezifische Pfade fest einzutragen. Getestet wird am iBMC.
- README und Oberfläche auf Englisch mit deutscher Übersetzung. Dieser Plan bleibt deutsch.
- Unterlagen zum Runner-Server (nicht öffentlich) werden nicht eingecheckt.
- Releases mit Versionsnummern, damit HACS Updates anbieten kann.

---

## 9. Umsetzung in Phasen

Jede Phase ist für sich nutzbar und einzeln abnehmbar.

**Phase 0 – Prüfungen am Server (vor dem ersten Code, etwa eine Stunde vor Ort)**

- iBMC: eigenen Benutzer für Home Assistant anlegen und die Redfish-Pfade für Zustand, Einschalten und Leistung
  einmal von Hand abrufen (`https://<ibmc>/redfish/v1/Systems`, `…/Chassis/…/Power`).
- Messen über iBMC: Leistung im Leerlauf, Leistung ausgeschaltet, Zeit vom Einschalten bis alle Runner online sind.
- Probe: per iBMC sauber aus- und wieder einschalten, kommen alle Runner und das Dashboard von selbst?
- BIOS: Verhalten nach Stromausfall.
- Warnlogik der Dashboard-Skripte lesen: Wie entsteht die Warnung „Runner offline“, und feuert sie rund um einen geplanten Neustart?
- Klären, auf welchem Runner der erste Job der Nacht-Suite läuft.

**Phase 1 – Gerüst und Planer.** Config-Flow, GitHub-Client, Zeitpläne, Dispatch mit Wiederholung, Entitäten je
Zeitplan. Ergebnis: Die Nacht-Suite startet pünktlich um 03:00 Uhr, unabhängig vom Energiethema. Der Server läuft
weiter durch.

**Phase 2 – Beobachten.** Warteschlangen-Polling, iBMC-Zustand und Leistung, Wächter-Befehl `status`,
Zustandsautomat im Trockenlauf. Ergebnis: ein paar Tage Protokoll, wann geweckt und abgeschaltet worden wäre, und
echte Zahlen zur möglichen Ersparnis. Damit lässt sich X begründet wählen.

**Phase 3 – Wecken.** Einschalten per iBMC bei wartendem Job, vor Zeitplänen und per Knopf; Bereitschaftsprüfung,
Weckfehler mit Benachrichtigung. Von Hand ausgeschaltet wird in dieser Phase noch selbst.

**Phase 4 – Herunterfahren.** Wächter-Befehl `shutdown`, Leerlauffristen, Sperren, Schleifenschutz,
Pflicht-Wecken, Anpassungen an Dashboard-Warnungen und Status-Issue.

**Phase 5 – Abrunden.** Optionaler Webhook, HACS-Veröffentlichung mit Releases, Diagnose-Export, Dokumentation.

**Tests:** Zustandsautomat und Planer vollständig mit simulierter Zeit (inklusive Sommerzeit-Umstellung, HA-Neustart
um 03:00 Uhr, GitHub nicht erreichbar, Wächter lehnt ab). Das Wächter-Skript bekommt eigene Tests mit nachgestellten
Containern. Am echten Server wird zuerst im Trockenlauf geprüft.

---

## 10. Was es ungefähr bringt

Noch ohne Messung, nur zur Einordnung. Angenommen 100 W Unterschied zwischen Leerlauf und ausgeschaltet und ein
Server, der werktags etwa 11 Stunden gebraucht wird: Dann wäre er rund 110 von 168 Wochenstunden aus. Das sind
etwa 11 kWh je Woche, rund 570 kWh im Jahr, bei 0,32 €/kWh etwa 180 € im Jahr. Mit zusätzlich bestellten, stärkeren CPUs
dürfte der Leerlaufwert etwas steigen, die Ersparnis also eher wachsen. Die echten Werte liefert Phase 2.

---

## 11. Entschieden und offen

Entschieden am 16.11.2026:

- X ist einstellbar und wird von Hand nachjustiert.
- Der iBMC unterstützt Redfish; Home Assistant liegt im selben Netz wie der iBMC.
- Das Wächter-Skript samt SSH-Schlüssel und sudoers-Zeile darf auf den Server.

- Home Assistant läuft direkt auf einem Thin Client (HA OS). Installation also über HACS, ohne Sonderwege.
- Beobachtet werden alle Repositories der Organisation.
- Vor der Nacht-Suite wird der Server vorab geweckt.
- Das Repository wird öffentlich und über HACS nutzbar.

Noch offen:

1. Lizenz: Vorschlag MIT.
2. Die Punkte aus Phase 0, die nur am Server zu klären sind (Messwerte, Verhalten der Dashboard-Warnungen,
   Runner des ersten Nacht-Suite-Jobs).
