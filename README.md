# AlphaESS Smile-B3-Batteriespeicher und EVCT11-Wallbox per EVCC steuern

Zwei zusammengehörige Projekte, um ein AlphaESS-System **direkt über
[EVCC](https://evcc.io)** zu steuern — ohne AlphaESS-Cloud:

- **[Teil A: EVCT11-Wallbox](#teil-a-evct11-wallbox)** — Reverse-Engineering
  des internen RS485-Modbus-Protokolls zwischen der AlphaESS
  SMILE-B3(-PLUS)-Batterie und der zugehörigen EVCT11-Wallbox, plus eine
  MQTT-Brücke, mit der EVCC die Wallbox unabhängig von der B3 steuert.
- **[Teil B: Smile-B3-Batteriespeicher](#teil-b-smile-b3-batteriespeicher)** —
  den Speicher selbst (in meinem Fall ein Storion Smile B3) per Modbus in EVCC
  einbinden: auslesen, Entladung sperren, Netzladen. Dafür bringt EVCC ein
  eigenes Template mit, ein eigenes Skript ist nicht nötig.

Beide Geräte sprechen Modbus RTU über RS485. Wie man sie anschließt — per
USB-RS485-Adapter oder per RS485-Ethernet-Konverter — steht gemeinsam für
beide Teile unter [Hardware-Anbindung](#hardware-anbindung).

**Wie beides zusammenhängt:** Solange die EVCT11 in der B3-Konfiguration
eingetragen ist, kommuniziert sie ausschließlich mit dem Batteriesystem. EVCC
kann sie dann nicht ansteuern — und den B3 auch nicht, denn der ist in dieser
Konstellation selbst Modbus-Master und antwortet auf keine Anfrage. Erst nach
Entfernen der Wallbox aus der B3-Konfiguration (durch Installateur oder
AlphaESS-Support) lässt sich der B3 per EVCC ansteuern (Teil B). Die Wallbox
verliert damit ihren einzigen Kommunikationspartner — Teil A liefert den
Ersatz dafür.

⚠️ **Kein offizielles AlphaESS-Projekt. Nutzung auf eigene Gefahr.** Das
Wallbox-Protokoll wurde durch passives Mitschneiden und aktives Experimentieren
ermittelt, nicht aus offizieller Dokumentation. Insbesondere die Register für
Sollwert-Steuerung wurden empirisch kalibriert (siehe unten) — Fehler in der
eigenen Verkabelung/Konfiguration können im ungünstigsten Fall zu falschem
Ladeverhalten führen. Immer mit Aufsicht testen.

## Inhalt

- [Hardware-Anbindung](#hardware-anbindung)
- [Teil A: EVCT11-Wallbox](#teil-a-evct11-wallbox)
- [Teil B: Smile-B3-Batteriespeicher](#teil-b-smile-b3-batteriespeicher)
- [Dateien in diesem Repo](#dateien-in-diesem-repo)
- [Danksagung / verwandte Projekte](#danksagung--verwandte-projekte)

---

## Hardware-Anbindung

Gilt für beide Teile. Wallbox und Speicher hängen an **getrennten**
RS485-Bussen — jeder Bus hat genau einen Master:

| | Bus zur Wallbox (Teil A) | Bus zum B3 (Teil B) |
|---|---|---|
| Master | die MQTT-Brücke aus diesem Repo | EVCC |
| Slave | EVCT11, Unit-ID **1** | Smile-B3, Slave-ID **85** (0x55) |
| Schnittstelle | 9600 Baud, 8N1 | 9600 Baud, 8N1 |

Für jeden der beiden Busse gibt es zwei Wege zum Rechner, auf dem
EVCC bzw. die Brücke läuft.

### Weg 1: USB-RS485-Adapter

Ein **einfacher USB-RS485-Adapter reicht aus** — kein spezielles
"industrial-grade"-Gerät mit Terminierungs-/Bias-Schaltern nötig. Die
funktionierende Lösung in diesem Repo läuft mit einem simplen Adapter ohne
120Ω-Terminierung und ohne extra Masseleitung.

Tipp: Den Adapter über `/dev/serial/by-id/...` ansprechen statt über
`/dev/ttyUSB0` — der Pfad bleibt gleich, auch wenn sich die Reihenfolge der
USB-Geräte ändert.

### Weg 2: RS485-Ethernet-Konverter

Statt eines USB-Adapters hängt der Bus an einem kleinen RS485-zu-Ethernet-
Konverter; der Rechner erreicht ihn übers Netzwerk. Das spart USB-Kabel und
das Durchreichen von USB-Geräten in VMs/Container, und der Rechner muss nicht
neben Wallbox oder Speicher stehen.

Bei mir im Einsatz: **Waveshare "2-CH RS485 TO ETH (B)"**. Das Gerät hat zwei
unabhängige RS485-Kanäle; jeder Kanal hat seine eigene IP-Adresse und wird
getrennt konfiguriert. Wallbox und B3 hängen also an je einem Kanal und
werden über zwei verschiedene IP-Adressen angesprochen (beide auf dem
Standard-Port 4196). Bei mir: Kanal 1 → B3, Kanal 2 → Wallbox.

Einstellungen je Kanal:

- Arbeitsmodus: **TCP Server**
- Übertragung: **transparent** (keine Umsetzung "Modbus TCP to RTU" — der
  Konverter reicht die RTU-Bytes nur durch)
- Seriell: **9600 Baud, 8 Datenbits, keine Parität, 1 Stoppbit**
- Port: 4196 (Standard)

### Was sich in der Software ändert

| | USB-RS485-Adapter | RS485-Ethernet-Konverter |
|---|---|---|
| Python-Skripte in diesem Repo | `--port /dev/serial/by-id/...` | `--port socket://IP:4196` |
| EVCC (Teil B) | Verbindung "RS485": Gerätepfad, 9600, 8N1 | Verbindung "Netzwerk": IP + Port, Protokoll **RTU** (nicht TCP) |

Die Python-Skripte öffnen die Verbindung mit `serial.serial_for_url()` statt
`serial.Serial()`. pyserial erkennt `socket://`-URLs selbst, ein normaler
Gerätepfad funktioniert damit unverändert weiter — derselbe Code deckt also
beide Wege ab:

```python
# vorher: nur serieller Adapter
ser = serial.Serial(port="/dev/ttyUSB0", baudrate=9600, timeout=0.02)

# nachher: Gerätepfad ODER socket://-URL
ser = serial.serial_for_url("socket://xxx.xxx.x.xxx:4196", baudrate=9600, timeout=0.02)
```

Zwei Dinge sind bei `socket://` anders: Baudrate und Parität haben im Skript
keine Wirkung mehr (sie werden am Konverter eingestellt), und eine
Netzwerkverbindung kann abreißen. Ein Dauerläufer muss sich dann neu
verbinden — die MQTT-Brücke tut das (siehe
[Betrieb der Brücke](#betrieb-der-brücke)).

### Wenn keine Antwort kommt

In dieser Reihenfolge prüfen:

1. **Stimmen die Master/Slave-Rollen?** Das war in beiden Projekten die
   eigentliche Ursache (siehe unten). Zwei Master auf einem Bus oder zwei
   wartende Slaves reden nie miteinander.
2. **A/B-Polarität korrekt?** Bei Bitfehlern wie z.B. `0xFE`, das den
   Bytestream dominiert, oder wenn gar nichts zurückkommt: A/B einfach
   vertauschen. Das gilt für den USB-Adapter genauso wie für den Konverter.
3. **9600, 8N1** — und beim Konverter: TCP-Server-Modus, transparente
   Übertragung, richtiger Kanal bzw. richtige IP.

⚠️ **Lessons Learned / Sackgasse, die Zeit gekostet hat:** Ein Großteil der
ursprünglichen Fehlersuche in diesem Projekt drehte sich um vermeintlich
fehlende Terminierung, Bias-Widerstände und eine gemeinsame Masse — mit
entsprechendem Hardware-Aufwand (zusätzlicher Dongle mit schaltbarem
120Ω-Widerstand etc.). **Das war komplett unnötig.** Die eigentliche Ursache
war die falsche Annahme über die Master/Slave-Rollen — sobald
klar war, dass die eigene Software den Master spielen muss statt passiv auf
die Wallbox zu warten, funktionierte auch der einfachste Dongle sofort
zuverlässig. Bevor Ihr in speziellere Hardware investiert: Prüft zuerst, ob
Eure Software wirklich als Master pollt.

Dieselbe Falle gab es beim Speicher noch einmal, nur andersherum: Dort
blieben alle Leseversuche ohne Antwort, weil der B3 selbst Master war
(siehe [Teil B](#teil-b-smile-b3-batteriespeicher)).

---

## Teil A: EVCT11-Wallbox

Ziel: die Wallbox **direkt über EVCC** steuern — unabhängig von der B3 und
ohne AlphaESS-Cloud.

### Ausgangslage / Setup

- AlphaESS SMILE-B3(-PLUS) + EVCT11-Wallbox, verbunden über RS485 (RJ45,
  Pins 4/5, "4B5A")
- Ein einfacher USB-RS485-Dongle oder ein RS485-Ethernet-Konverter ersetzt
  die B3 aus Sicht der Wallbox (siehe [Hardware-Anbindung](#hardware-anbindung)
  — weniger Hardware-Aufwand nötig, als man zunächst vermuten könnte)
- EVCC läuft als Home-Assistant-Add-on, angebunden über MQTT (nicht direkt
  per Modbus-Plugin — siehe [Warum MQTT-Brücke](#warum-eine-mqtt-brücke-statt-evcc-direkt-per-modbus))

### Rollen

- Der **B3 ist Master** (Unit-ID 1 aus ihrer Sicht als Ziel-Adresse) und
  pollt die Wallbox alle ~200ms
- Die **Wallbox ist Slave** (Unit-ID 1) und antwortet nur auf Anfragen —
  sie sendet nie von sich aus etwas

Wer die Wallbox ohne B3 betreiben will, muss also selbst den Master spielen.
Genau das tut `wallbox_mqtt_bridge.py`.

### Register-Übersicht (empirisch ermittelt)

Alle Adressen sind absolute Modbus-Register-Adressen (nicht 40001-Basis).

#### Lese-Block: `0x0091` (dezimal 145), Read Holding Registers, 19 Register

Wird von der B3 alle ~200ms abgefragt; die Wallbox antwortet mit ihrer
eigenen Telemetrie (sie hat die reale AC-Strommessung):

| Offset | Adresse | Bedeutung | Einheit |
|---|---|---|---|
| 0 | 0x91 | Netzspannung L1 | ÷10 → V |
| 1 | 0x92 | Ladestrom | ÷10 → A |
| 3 | 0x94 | **Ladeleistung** (Ist-Wert, exakt validiert) | W |
| 6 | 0x97 | Netzspannung L2 | ÷10 → V |
| 7 | 0x98 | Boolean: lädt aktiv | 0/1 |
| 8 | 0x99 | Netzspannung L3 | ÷10 → V |
| 9 | 0x9A | Boolean: lädt aktiv (redundant zu Offset 7?) | 0/1 |
| 14 | 0x9F | Ändert sich je nach Verbindungsstatus | ? |
| **16** | **0xA1** | **Fahrzeug verbunden** (~8 = nein, ~144-161 = ja) | — |
| 17 | 0xA2 | Kumulativer Zähler | — |
| 18 | 0xA3 | Kumulativer Zähler | — |

**Wichtig:** Register 16 (nicht 9, wie eine frühe Vermutung nahelegte) ist
der zuverlässige "Fahrzeug verbunden"-Indikator — unabhängig vom Ladezustand.

#### Discovery-Block: `0x0001`, 38 Register

Wird von der B3 einmal pro Sekunde abgefragt, enthält eine ASCII-Geräte-ID
der Wallbox (z.B. `ALPxxxxxxxxx` o.ä. — Modellcode/Firmware-Version-artig).

#### Schreib-Block: `0x0100` (dezimal 256), Write Multiple Registers, 9 Register

Wird von der B3 etwa alle 2 Sekunden in die Wallbox geschrieben — **das ist
der Steuerkanal**:

| Offset | Adresse | Bedeutung |
|---|---|---|
| 1 | 0x101 | **Sollwert** — steuert die Ladeleistung, siehe Kalibrierung unten |
| 3, 5, 7 | 0x103, 0x105, 0x107 | Vermutlich EMS-Telemetrie (PV-Erzeugung o.ä.), feste Werte 4000/7000/7000 funktionieren in der Praxis |
| 8 | 0x108 | 1 = Leerlauf/kein Auto, 0 = Auto verbunden (Normalbetrieb) |

#### Kalibrierung des Sollwert-Registers (0x101)

Empirisch per systematischem Sweep ermittelt (Fahrzeug: 1-phasig ladend):

- **R1 ≤ 4200 → keine Ladung**
- **R1 = 4300 → Aktivierungsschwelle** (≈ 6A × 3 Phasen × 230V — passt zur
  IEC61851-Mindeststromvorgabe pro Phase, auch bei 1-phasigem Laden scheint
  die Wallbox alle 3 angeschlossenen Phasen zu prüfen)
- **R1 = 4300–10500 → nahezu linear regelbar**, grober Fit:
  `R1 ≈ 652 × Ampere + 1239`
- **R1 ≥ 12000 → Sättigung** nahe des Fahrzeug-eigenen OBC-Maximums (nicht
  zuverlässig reproduzierbar — abhängig vom Fahrzeugzustand, nicht von der
  Wallbox)

**Unbestätigte Annahme:** Der Sollwert gilt vermutlich PRO PHASE (passend zu
IEC61851-CP-PWM-Semantik). Ein 3-phasig ladendes Fahrzeug sollte bei
gleichem R1-Wert die dreifache Leistung ziehen können — mangels
3-phasigem Testfahrzeug nicht verifiziert. **Feedback von jemandem mit
einem 3-phasigen Auto sehr willkommen!**

### Warum eine MQTT-Brücke (statt EVCC direkt per Modbus)?

EVCC unterstützt zwar generische Modbus-Charger-Konfiguration, aber:
- Die genaue Syntax für Schreib-Operationen (Skalierung, Bool→Wert-Mapping)
  ist nicht vollständig dokumentiert
- Eine MQTT-Brücke gibt volle Kontrolle über die Umrechnung in Python, ohne
  auf EVCC-interne Plugin-Details angewiesen zu sein
- Passt zu vielen bestehenden Setups, die AlphaESS-Daten ohnehin schon über
  MQTT einbinden

Siehe `wallbox/wallbox_mqtt_bridge.py` für die Implementierung. Eine
vollwertige EVCC-Integration wäre super, für meine Zwecke aber erstmal nicht
nötig. Wenn sich da jemand dran versuchen möchte...

### Betrieb der Brücke

⚠️ Die Brücke sendet **aktiv** auf dem RS485-Bus. Nur verwenden, wenn die
echte B3 physisch von diesem Bus getrennt ist.

```bash
# USB-RS485-Adapter
python3 wallbox/wallbox_mqtt_bridge.py \
    --port /dev/serial/by-id/HIER_DEINEN_AKTUELLEN_DONGLE_EINTRAGEN --baud 9600 \
    --mqtt-host xxx.xxx.x.xxx --mqtt-prefix alphaess/wallbox

# RS485-Ethernet-Konverter
python3 wallbox/wallbox_mqtt_bridge.py \
    --port socket://xxx.xxx.x.xxx:4196 \
    --mqtt-host xxx.xxx.x.xxx --mqtt-prefix alphaess/wallbox
```

Die MQTT-Zugangsdaten übergibt man am besten über die Umgebungsvariablen
`MQTT_USER` und `MQTT_PASS` (so stehen sie nicht in der Prozessliste),
alternativ per `--mqtt-user` / `--mqtt-pass`. Für den Dauerbetrieb
liegen eine systemd-Unit und eine Vorlage für die Zugangsdaten-Datei bei;
die ausgefüllte Datei gehört nach `/etc/wallbox-mqtt-bridge.env` (nur für
root lesbar) und nicht ins Repo.

MQTT-Topics (Prefix frei wählbar):

| Topic | Richtung | Inhalt |
|---|---|---|
| `<prefix>/status` | Brücke → EVCC | `A` (kein Fahrzeug), `B` (verbunden), `C` (lädt) |
| `<prefix>/enabled` | Brücke → EVCC | `true` / `false` |
| `<prefix>/power` | Brücke → EVCC | Ladeleistung in Watt |
| `<prefix>/set/enabled` | EVCC → Brücke | `true` / `false` |
| `<prefix>/maxcurrent` | EVCC → Brücke | Ampere pro Phase |

**Verbindungsabbrüche:** Reißt die Verbindung zur Wallbox ab (Adapter
gezogen, Konverter oder Netzwerk weg) oder kommt 30 Sekunden lang keine
gültige Antwort, baut die Brücke die Verbindung selbstständig neu auf und
versucht es alle 5 Sekunden erneut (einstellbar über `--reconnect-after` und
`--reconnect-delay`). Solange die Verbindung fehlt, bleiben auf MQTT die
zuletzt gelesenen Werte stehen; im Log steht dann `bus=GETRENNT`.

### Nicht geklärte offene Punkte

- Bedeutung der Register 0x103/0x105/0x107 im Schreib-Block
- Exakte Bedeutung von Register 9 vs. 7 (beide "lädt aktiv"?)
- Verifikation der Pro-Phase-Annahme mit einem 3-phasigen Fahrzeug

---

## Teil B: Smile-B3-Batteriespeicher

Ziel: den Speicher in EVCC auslesen und steuern — SOC und Leistung anzeigen,
die Entladung der Batterie sperren, während ein Auto lädt, und gezielt aus
dem Netz laden. Die Entladesperre ist der eigentliche Anlass: Wer günstigen
Netzstrom ins Auto lädt, will dabei nicht den Batteriespeicher leerziehen.

### Ausgangslage

Solange eine EVCT11-Wallbox in der B3-Konfiguration eingetragen ist, ist der
B3 auf seiner RS485-Schnittstelle **Master** (er pollt die Wallbox, siehe
Teil A). In diesem Zustand blieb jeder Leseversuch von außen ohne Antwort —
nicht wegen Verkabelung, Terminierung oder Adresse, sondern weil ein Master
keine Anfragen beantwortet.

Die Lösung: Wallbox aus der B3-Konfiguration entfernen und vom Bus trennen;
der AlphaESS-Support hat den B3 danach auf den **Slave-Modus** umgestellt.
Seitdem antwortet er auf Modbus-Anfragen.

### Rollen

- **EVCC ist Master** und fragt den Speicher ab
- Der **B3 ist Slave** mit der Slave-ID **85** (0x55), 9600 Baud, 8N1

### Einrichtung in EVCC

EVCC kennt den Speicher über das mitgelieferte Template `alpha-ess-smile`
(in der Oberfläche: Alpha ESS → Storion SMILE). Die Einrichtung läuft
komplett über die EVCC-Oberfläche. Für die Verbindung gibt es dort zwei
Möglichkeiten, passend zu den beiden Wegen aus der
[Hardware-Anbindung](#hardware-anbindung):

| | USB-RS485-Adapter | RS485-Ethernet-Konverter |
|---|---|---|
| Verbindung | RS485 (seriell) | Netzwerk |
| Angaben | Gerätepfad, Baudrate 9600, ComSet 8N1 | IP-Adresse und Port des Konverter-Kanals |
| Protokoll | — | **RTU** auswählen, nicht TCP |
| Modbus-ID | 85 | 85 |
| in YAML | `device`, `baudrate`, `comset` | `uri` + `rtu: true` bzw. `modbus: rs485tcpip` |

Warum RTU und nicht TCP: Der Konverter arbeitet transparent, auf der Leitung
bleibt es Modbus RTU — nur eben in TCP verpackt. "TCP" würde echtes
Modbus TCP erwarten, das der Konverter in diesem Modus nicht spricht.

Dieselbe Konfiguration als YAML zum Nachlesen:
[`smile-b3/evcc-beispiel.yaml`](smile-b3/evcc-beispiel.yaml).

**Voraussetzung für die aktive Batteriesteuerung:** Im B3 müssen einmalig
Zeiten für das Netzladen hinterlegt sein (Einstellungen →
Funktionseinstellungen → Netzladen/Entladen), und zwar als durchgehender
Zeitraum — z.B. Ladezeit 1 von 00:00 bis 23:00 und Ladezeit 2 von 23:00 bis
00:00. Der Schalter "Netzladen" selbst bleibt aus; das eigentliche Schalten
übernimmt EVCC. Details in der
[EVCC-Dokumentation zum Gerät](https://docs.evcc.io/de/meters/alpha-ess-storion-smile).

### Was EVCC liest und schreibt

Alle Adressen sind absolute Modbus-Register-Adressen (Holding Register).

| Adresse | dezimal | Bedeutung | Format | von EVCC genutzt |
|---|---|---|---|---|
| 0x0021–0x0022 | 33 | Netzleistung | int32, W | lesen |
| 0x0100 | 256 | Batteriespannung | uint16, 0,1 V | — |
| 0x0101 | 257 | Batteriestrom | int16, 0,1 A | — |
| 0x0102 | 258 | **SOC** | uint16, 0,1 % | lesen |
| 0x0126 | 294 | **Batterieleistung** | int16, W (negativ = Laden) | lesen |
| 0x012C / 0x012D | 300 / 301 | max. Lade- / Entladeleistung | — | — |
| 0x080F | 2063 | Modbus-Adresse des B3 (Standard 0x55) | — | — |
| 0x0810 | 2064 | Baudrate des B3 | — | — |
| 0x084F | 2127 | **Steuerflag Zeitfenster** (Netzladen ein/aus) | uint16 | schreiben |
| 0x0851–0x0861 | 2129–2145 | Lade-/Entlade-Zeitfenster | — | — |
| 0x0855 | 2133 | **Ladestopp-SOC** | uint16, % | schreiben |
| ab 0x0880 | ab 2176 | "Power Dispatch Para1–8" | — | nicht verwendet |

Die Skalierung von Spannung und Strom stammt aus der Registerliste von
[Alpha2MQTT](https://github.com/dxoverdy/Alpha2MQTT/blob/master/Alpha2MQTT/Definitions.h);
EVCC liest diese beiden Register nicht.

Nicht verwechseln: `0x0100` ist hier ein Lese-Register des Speichers — bei
der Wallbox (Teil A) ist dieselbe Adresse der Schreib-Block.

So setzt das Template die drei Batterie-Modi von EVCC um:

| Modus in EVCC | 0x084F | 0x0855 | Wirkung |
|---|---|---|---|
| normal | 0 | — | Speicher arbeitet wie gewohnt |
| halten | 1 | minimaler SOC | "Netzladen" mit niedrigem Ziel-SOC: es wird nicht geladen, aber auch nicht entladen |
| laden | 1 | maximaler SOC | Netzladen, EVCC beendet es wieder |

Im Betrieb bestätigt: Die Entladung wird gesperrt, wenn ein Auto lädt,
Netzladen über EVCC funktioniert, und die angezeigten Werte stimmen mit dem
AlphaESS-eigenen Monitoring überein.

### Verbindung testen, ohne EVCC

Sinnvoll vor der Einrichtung in EVCC oder vor dem Umstieg von USB auf den
Konverter. Währenddessen sollte nichts anderes den B3 abfragen (EVCC kurz
stoppen) — RS485 verträgt nur einen Master.

**Minimaltest über den Konverter**, nur mit Bordmitteln (bash):

```bash
./smile-b3/b3_tcp_probe.sh xxx.xxx.x.xxx 4196
```

Das Skript schickt eine einzige rohe Leseanfrage für den SOC
(`55 03 01 02 00 01 29 e2`) und prüft die Antwort. So sieht es aus, wenn
alles stimmt:

```
Anfrage : 55 03 01 02 00 01 29 e2   (Slave 0x55, Lesen, Register 0x0102, 1 Register)
Antwort : 55 03 02 03 b8 89 0a
OK: CRC gültig, SOC = 95.2 %  (Rohwert 952, Skalierung 0,1 %)
```

**Mehrere Werte lesen**, über USB-Adapter oder Konverter (braucht pyserial):

```bash
python3 smile-b3/b3_read_test.py --port /dev/serial/by-id/HIER_DEINEN_ADAPTER_EINTRAGEN
python3 smile-b3/b3_read_test.py --port socket://xxx.xxx.x.xxx:4196
```

Beide Tools lesen nur, sie verändern nichts am Speicher.

---

## Dateien in diesem Repo

### `wallbox/` — Teil A

| Datei | Zweck |
|---|---|
| `rs485_capture.py` | Passiver Sniffer (Rohdaten + Zeitstempel) |
| `modbus_frame_decoder.py` | CRC-basierter Frame-Decoder für Mitschnitte |
| `b3_master_emulator.py` | Aktiver Modbus-Master (ersetzt die B3), inkl. automatischem Sweep-Modus zur Kalibrierung |
| `wallbox_mqtt_bridge.py` | Produktions-Brücke: Modbus ↔ MQTT für EVCC-Integration, mit automatischem Neuverbinden |
| `wallbox-mqtt-bridge.service` | systemd-Unit für Dauerbetrieb |
| `wallbox-mqtt-bridge.env.example` | Vorlage für die MQTT-Zugangsdaten der systemd-Unit |

Alle drei Skripte mit Bus-Zugriff nehmen bei `--port` einen Gerätepfad oder
eine `socket://IP:PORT`-URL.

### `smile-b3/` — Teil B

| Datei | Zweck |
|---|---|
| `b3_tcp_probe.sh` | Minimaltest über den RS485-Ethernet-Konverter (eine SOC-Abfrage, nur bash) |
| `b3_read_test.py` | Liest SOC, Batterieleistung, Spannung, Strom und Netzleistung — über USB-Adapter oder Konverter |
| `evcc-beispiel.yaml` | EVCC-Konfiguration für beide Anbindungswege als YAML |

Benötigte Pakete für die Python-Skripte: `python3-serial` (pyserial), für die
Brücke zusätzlich `paho-mqtt`.

## Danksagung / verwandte Projekte

Dieses Projekt wurde mit massiver Unterstützung durch
[Claude](https://claude.ai) (Anthropic) umgesetzt — von der Analyse der
Mitschnitte über die Skripte bis zu dieser Dokumentation.

- [EVCC](https://evcc.io) — das Template `alpha-ess-smile` macht die
  Batterie-Seite (Teil B) ohne eigenes Skript möglich
  ([Geräteseite in der EVCC-Dokumentation](https://docs.evcc.io/de/meters/alpha-ess-storion-smile))
- [Alpha2MQTT](https://github.com/dxoverdy/Alpha2MQTT) — spricht dieselbe
  externe B3-Schnittstelle (Slave-ID 85) und stellt die Daten per MQTT
  bereit. Eine Alternative für alle, die den Speicher ohne EVCC z.B. direkt
  in Home Assistant einbinden möchten; für die Steuerung über EVCC wird es
  nicht benötigt
- EVCC-Community, [Discussion #21869](https://github.com/evcc-io/evcc/discussions/21869)
  zu OCPP-Problemen mit der EVCT11

## Lizenz
MIT — freie Nutzung, keine Gewähr.
