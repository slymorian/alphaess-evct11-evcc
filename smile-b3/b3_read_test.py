#!/usr/bin/env python3
"""
b3_read_test.py — liest ein paar Kernwerte des AlphaESS Smile-B3 per
Modbus RTU und zeigt sie an. Reiner LESE-Test (nur Funktion 03), es wird
nichts am Speicher verändert.

Zweck: prüfen, ob Verkabelung, Adresse und Anbindung stimmen, bevor (oder
unabhängig davon, ob) EVCC eingerichtet ist. Funktioniert über beide
Anbindungswege, weil pyserial beides über serial_for_url() öffnet:

    USB-RS485-Adapter:
        python3 b3_read_test.py --port /dev/ttyUSB0

    RS485-Ethernet-Konverter (TCP-Server, transparente Übertragung;
    9600 8N1 werden dann am Konverter eingestellt):
        python3 b3_read_test.py --port socket://xxx.xxx.x.xxx:4196

Voraussetzungen:
  - Der B3 ist Modbus-SLAVE (Adresse 85 / 0x55). Solange er selbst Master
    ist (z.B. weil noch eine EVCT11-Wallbox eingetragen ist), antwortet er
    auf nichts — siehe README, Teil B.
  - Währenddessen fragt nichts anderes den B3 ab (EVCC kurz stoppen). RS485
    kennt nur einen Master; zwei gleichzeitige Abfrager stören sich.

Gelesene Register (absolute Adressen):
    0x0100  Batteriespannung      0,1 V
    0x0101  Batteriestrom         0,1 A  (vorzeichenbehaftet)
    0x0102  SOC                   0,1 %
    0x0126  Batterieleistung      W, vorzeichenbehaftet (negativ = Laden)
    0x0021  Netzleistung (2 Reg.) W, vorzeichenbehaftet (positiv = Bezug)

SOC, Batterie- und Netzleistung werden so auch vom EVCC-Template
"alpha-ess-smile" gelesen. Zu jedem Wert wird zusätzlich der Rohwert
ausgegeben.
"""
import argparse
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("Bitte zuerst installieren: apt install -y python3-serial")


def modbus_crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def with_crc(body: bytes) -> bytes:
    crc = modbus_crc16(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def crc_ok(frame: bytes) -> bool:
    return modbus_crc16(frame[:-2]) == (frame[-2] | (frame[-1] << 8))


class ModbusError(Exception):
    pass


def read_holding(ser, unit_id: int, addr: int, qty: int, timeout: float) -> list:
    """Liest qty Holding-Register ab addr. Gibt die Rohwerte (0..65535) zurück."""
    ser.reset_input_buffer()
    ser.write(with_crc(bytes([unit_id, 0x03]) + addr.to_bytes(2, 'big') + qty.to_bytes(2, 'big')))

    want = 3 + 2 * qty + 2
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        buf.extend(ser.read(64))
        # Frame-Anfang suchen (führende Störbytes überspringen)
        for i in range(len(buf)):
            if buf[i] != unit_id or i + 2 > len(buf):
                continue
            func = buf[i + 1]
            if func == 0x83 and len(buf) - i >= 5 and crc_ok(bytes(buf[i:i + 5])):
                raise ModbusError(f"Gerät meldet Modbus-Fehlercode 0x{buf[i + 2]:02X}")
            if func == 0x03 and len(buf) - i >= want and buf[i + 2] == 2 * qty:
                frame = bytes(buf[i:i + want])
                if crc_ok(frame):
                    data = frame[3:-2]
                    return [int.from_bytes(data[k:k + 2], 'big') for k in range(0, len(data), 2)]
    if buf:
        raise ModbusError(f"keine gültige Antwort (empfangen: {bytes(buf).hex(' ')})")
    raise ModbusError("keine Antwort")


def s16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def s32(hi: int, lo: int) -> int:
    v = (hi << 16) | lo
    return v - 0x100000000 if v & 0x80000000 else v


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True,
                    help="Serielles Gerät (/dev/ttyUSB0, /dev/serial/by-id/...) oder socket://IP:PORT")
    ap.add_argument("--baud", type=int, default=9600,
                    help="Baudrate (Standard: 9600; bei socket:// ohne Wirkung)")
    ap.add_argument("--unit-id", type=int, default=85,
                    help="Modbus-Adresse des B3 (Standard: 85 = 0x55)")
    ap.add_argument("--timeout", type=float, default=1.0,
                    help="Sekunden Wartezeit je Anfrage (Standard: 1.0)")
    args = ap.parse_args()

    try:
        ser = serial.serial_for_url(
            args.port, baudrate=args.baud,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE, stopbits=1,
            timeout=0.05,
        )
    except (serial.SerialException, OSError) as e:
        sys.exit(f"FEHLER: {args.port} lässt sich nicht öffnen: {e}")

    print(f"Lese von {args.port}, Modbus-Adresse {args.unit_id} (0x{args.unit_id:02X}) ...\n")

    # (Startadresse, Anzahl, Auswertung -> Liste von (Name, Text))
    blocks = [
        (0x0100, 3, lambda r: [
            ("Batteriespannung", f"{r[0] / 10:.1f} V   (roh {r[0]})"),
            ("Batteriestrom", f"{s16(r[1]) / 10:.1f} A   (roh {r[1]})"),
            ("SOC", f"{r[2] / 10:.1f} %   (roh {r[2]})"),
        ]),
        (0x0126, 1, lambda r: [
            ("Batterieleistung", f"{s16(r[0])} W   (negativ = Laden; roh {r[0]})"),
        ]),
        (0x0021, 2, lambda r: [
            ("Netzleistung", f"{s32(r[0], r[1])} W   (positiv = Bezug; roh {r[0]}, {r[1]})"),
        ]),
    ]

    failures = 0
    try:
        for addr, qty, decode in blocks:
            try:
                regs = read_holding(ser, args.unit_id, addr, qty, args.timeout)
            except ModbusError as e:
                failures += 1
                print(f"  0x{addr:04X} ({qty} Register): FEHLER — {e}")
                continue
            except (serial.SerialException, OSError) as e:
                sys.exit(f"FEHLER: Verbindung zu {args.port} abgebrochen: {e}")
            for name, text in decode(regs):
                print(f"  {name:<17} {text}")
            time.sleep(0.05)  # kurze Pause zwischen den Anfragen
    finally:
        ser.close()

    if failures == len(blocks):
        print("\nKeine einzige Antwort. Typische Ursachen:\n"
              "  - A/B-Adern vertauscht (einfach tauschen und erneut testen)\n"
              "  - B3 ist (noch) Modbus-Master statt Slave (siehe README, Teil B)\n"
              "  - falsche Modbus-Adresse (--unit-id) oder nicht 9600 8N1\n"
              "  - Konverter nicht im transparenten TCP-Server-Modus",
              file=sys.stderr)
        sys.exit(1)
    if failures:
        sys.exit(1)
    print("\nOK — der B3 antwortet als Modbus-Slave.")


if __name__ == "__main__":
    main()
