# AlphaESS EVCT11 Wallbox per EVCC steuern (unabhängig von der SMILE-B3)

Reverse-Engineering des internen RS485-Modbus-Protokolls zwischen der
AlphaESS SMILE-B3(-PLUS)-Batterie und der zugehörigen EVCT11-Wallbox, mit
dem Ziel, die Wallbox **direkt über [EVCC](https://evcc.io)** zu steuern —
unabhängig von der B3 und ohne AlphaESS-Cloud.

**Hintergrund:** Solange die EVCT11 in der B3-Konfiguration eingetragen ist,
kommuniziert sie ausschließlich mit der B3 (kein eigenes WLAN). EVCC kann sie
dann nicht ansteuern. Nach Entfernen der Wallbox aus der B3-Konfiguration
(durch den AlphaESS-Support) verliert sie ihren einzigen Kommunikationspartner
— dieses Projekt liefert den Ersatz dafür.

⚠️ **Kein offizielles AlphaESS-Projekt. Nutzung auf eigene Gefahr.** Dieses
Protokoll wurde durch passives Mitschneiden und aktives Experimentieren
ermittelt, nicht aus offizieller Dokumentation. Insbesondere die Register für
Sollwert-Steuerung wurden empirisch kalibriert (siehe unten) — Fehler in der
eigenen Verkabelung/Konfiguration können im ungünstigsten Fall zu falschem
Ladeverhalten führen. Immer mit Aufsicht testen.

## Ausgangslage / Setup

- AlphaESS SMILE-B3(-PLUS) + EVCT11-Wallbox, verbunden über RS485 (RJ45,
  Pins 4/5, "4B5A")
- Ein zusätzlicher, einfacher USB-RS485-Dongle ersetzt die B3 aus Sicht der
  Wallbox (siehe [Elektrische Voraussetzungen](#elektrische-voraussetzungen)
  — weniger Hardware-Aufwand nötig, als man zunächst vermuten könnte)
- EVCC läuft als Home-Assistant-Add-on, angebunden über MQTT (nicht direkt
  per Modbus-Plugin — siehe [Warum MQTT-Brücke](#warum-eine-mqtt-brücke))

## Die zentrale Erkenntnis: Rollen sind vertauscht

Naheliegend wäre die Annahme, dass die Wallbox als Modbus-Master die B3
abfragt. **Das Gegenteil ist der Fall:**

- Die **B3 ist Master** (Unit-ID 1 aus ihrer Sicht als Ziel-Adresse), sie
  pollt die Wallbox alle ~200ms
- Die **Wallbox ist Slave** (Unit-ID 1) und antwortet nur auf Anfragen —
  sie sendet nie von sich aus etwas

Das erklärt auch, warum ein simpler "Slave-Emulator" (der nur passiv auf
Anfragen der Wallbox wartet) nie funktioniert hat: Die Wallbox wartet ihrerseits
auf einen Master, der sie anspricht. Zwei passive Teilnehmer reden nie
miteinander.

## Elektrische Voraussetzungen

Ein **einfacher USB-RS485-Adapter reicht aus** — kein spezielles
"industrial-grade"-Gerät mit Terminierungs-/Bias-Schaltern nötig. Die
funktionierende Lösung in diesem Repo läuft mit einem simplen Adapter ohne
120Ω-Terminierung und ohne extra Masseleitung.

Worauf es ankommt:
- **A/B-Polarität korrekt** (bei Bitfehlern wie z.B. `0xFE`, das den
  Bytestream dominiert, A/B einfach vertauschen)
- Baudrate: **9600, 8N1**. Interne Unit-ID: **1**

⚠️ **Lessons Learned / Sackgasse, die Zeit gekostet hat:** Ein Großteil der
ursprünglichen Fehlersuche in diesem Projekt drehte sich um vermeintlich
fehlende Terminierung, Bias-Widerstände und eine gemeinsame Masse — mit
entsprechendem Hardware-Aufwand (zusätzlicher Dongle mit schaltbarem
120Ω-Widerstand etc.). **Das war komplett unnötig.** Die eigentliche Ursache
war die falsche Annahme über die Master/Slave-Rollen (siehe oben) — sobald
klar war, dass die eigene Software den Master spielen muss statt passiv auf
die Wallbox zu warten, funktionierte auch der einfachste Dongle sofort
zuverlässig. Bevor Ihr in speziellere Hardware investiert: Prüft zuerst, ob
Eure Software wirklich als Master pollt.

## Register-Übersicht (empirisch ermittelt)

Alle Adressen sind absolute Modbus-Register-Adressen (nicht 40001-Basis).

### Lese-Block: `0x0091` (dezimal 145), Read Holding Registers, 19 Register

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

### Discovery-Block: `0x0001`, 38 Register

Wird von der B3 einmal pro Sekunde abgefragt, enthält eine ASCII-Geräte-ID
der Wallbox (z.B. `ALP202104110` o.ä. — Modellcode/Firmware-Version-artig).

### Schreib-Block: `0x0100` (dezimal 256), Write Multiple Registers, 9 Register

Wird von der B3 etwa alle 2 Sekunden in die Wallbox geschrieben — **das ist
der Steuerkanal**:

| Offset | Adresse | Bedeutung |
|---|---|---|
| 1 | 0x101 | **Sollwert** — steuert die Ladeleistung, siehe Kalibrierung unten |
| 3, 5, 7 | 0x103, 0x105, 0x107 | Vermutlich EMS-Telemetrie (PV-Erzeugung o.ä.), feste Werte 4000/7000/7000 funktionieren in der Praxis |
| 8 | 0x108 | 1 = Leerlauf/kein Auto, 0 = Auto verbunden (Normalbetrieb) |

### Kalibrierung des Sollwert-Registers (0x101)

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

## Warum eine MQTT-Brücke (statt EVCC direkt per Modbus)?

EVCC unterstützt zwar generische Modbus-Charger-Konfiguration, aber:
- Die genaue Syntax für Schreib-Operationen (Skalierung, Bool→Wert-Mapping)
  ist nicht vollständig dokumentiert
- Eine MQTT-Brücke gibt volle Kontrolle über die Umrechnung in Python, ohne
  auf EVCC-interne Plugin-Details angewiesen zu sein
- Passt zu vielen bestehenden Setups, die AlphaESS-Daten ohnehin schon über
  MQTT einbinden

Siehe `wallbox_mqtt_bridge.py` für die Implementierung.

## Dateien in diesem Repo

| Datei | Zweck |
|---|---|
| `rs485_capture.py` | Passiver Sniffer (Rohdaten + Zeitstempel) |
| `modbus_frame_decoder.py` | CRC-basierter Frame-Decoder für Mitschnitte |
| `b3_master_emulator.py` | Aktiver Modbus-Master (ersetzt die B3), inkl. automatischem Sweep-Modus zur Kalibrierung |
| `wallbox_scanner.py` | Scannt einen Adressbereich nach unbekannten Registern |
| `wallbox_mqtt_bridge.py` | Produktions-Brücke: Modbus ↔ MQTT für EVCC-Integration |
| `wallbox-mqtt-bridge.service` / `.env` | systemd-Unit für Dauerbetrieb |

## Nicht geklärte offene Punkte

- Bedeutung der Register 0x103/0x105/0x107 im Schreib-Block
- Exakte Bedeutung von Register 9 vs. 7 (beide "lädt aktiv"?)
- Verifikation der Pro-Phase-Annahme mit einem 3-phasigen Fahrzeug
- Verhalten bei mehreren gleichzeitig ladenden Wallboxen an einer B3 (falls vorhanden)

## Danksagung / verwandte Projekte

- [Alpha2MQTT](https://github.com/dxoverdy/Alpha2MQTT) — spricht die externe
  B3-Schnittstelle (Slave-ID 85), relevant für die B3-Batterie-Seite nach
  Entfernung der Wallbox
- EVCC-Community, [Discussion #21869](https://github.com/evcc-io/evcc/discussions/21869)
  zu OCPP-Problemen mit der EVCT11

## Lizenz

Vorschlag: MIT — freie Nutzung, keine Gewähr. (Nach eigenem Ermessen anpassen.)
