#!/usr/bin/env python3
"""
b3_master_emulator.py — Zweite Version des B3-Emulators, diesmal als
Modbus-RTU-MASTER statt Slave.

Hintergrund: Ein isolierter Mitschnitt mit NUR der echten B3 (Wallbox
abgezogen) zeigte, dass die B3 selbst aktiv "01 03 0001 0026"-Anfragen
sendet — sie tritt also als MASTER auf, nicht als Slave, wie ursprünglich
angenommen. Das deutet darauf hin, dass die WALLBOX die Slave-Rolle hat
(Unit-ID 1) und auf Anfragen der B3 wartet, statt selbst zu pollen.

Dieses Skript emuliert die B3 in dieser (vermutlich korrekten) Rolle:
  - Sendet periodisch Read Holding Registers an Unit-ID 1:
      * 0x0091, 19 Register (bekannter Telemetrie-Block)
      * 0x0001, 38 Register (das neu entdeckte "Discovery"-Read)
  - Wartet zusätzlich auf eingehende Write Multiple Registers am
    0x0100-Block (falls die Wallbox — nun als Slave — solche Writes
    unaufgefordert sendet) und beantwortet sie mit dem korrekten Echo.

=======================================================================
SICHERHEITSHINWEIS
=======================================================================
Dieses Skript sendet AKTIV auf dem RS485-Bus. Nur verwenden, wenn die
echte B3 physisch vom Bus getrennt ist! Zuerst OHNE angestecktes
Fahrzeug testen.
=======================================================================

Beispiel:
    python3 b3_master_emulator.py --port /dev/ttyUSB0 --baud 9600
"""
import argparse
import json
import os
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("Bitte zuerst installieren: apt install -y python3-serial")

# Bekanntes Leerlauf-Muster (aus echten Mitschnitten): alle Register 0,
# nur das letzte (Offset 8, absolute Adresse 0x0108) = 1.
DEFAULT_WRITE_REGS = [0, 0, 0, 0, 0, 0, 0, 0, 1]


class OverrideStore:
    """Lädt eine JSON-Datei mit den 9 Registerwerten für den 0x0100-Write
    (Liste von 9 Zahlen), neu bei jeder Änderung. Fehlt die Datei, wird
    das bekannte, sichere Leerlauf-Muster verwendet."""
    def __init__(self, path):
        self.path = path
        self.mtime = None
        self.regs = list(DEFAULT_WRITE_REGS)

    def get(self):
        if not self.path or not os.path.exists(self.path):
            return self.regs
        mtime = os.path.getmtime(self.path)
        if mtime != self.mtime:
            try:
                with open(self.path) as f:
                    raw = json.load(f)
                if isinstance(raw, list) and len(raw) == 9:
                    self.regs = [int(v) for v in raw]
                    self.mtime = mtime
                    print(f"[{time.strftime('%H:%M:%S')}] Write-Override geladen: {self.regs}")
                else:
                    print(f"[WARN] Override-Datei muss eine Liste von genau 9 Zahlen sein, ignoriere.", file=sys.stderr)
            except Exception as e:
                print(f"[WARN] Override-Datei fehlerhaft, ignoriere: {e}", file=sys.stderr)
        return self.regs


class SweepController:
    """Testet automatisch eine Liste von R1-Werten (Register 0x0101) der
    Reihe nach durch, hält jeden für --sweep-dwell Sekunden, sammelt dabei
    die von der Wallbox gemeldeten Ist-Werte (Strom/Leistung aus dem
    0x91-Lese-Block) und mittelt nur über die STABILE zweite Hälfte jedes
    Zeitfensters (um Einschwing-/Rampeneffekte auszuschließen).
    R3/R5/R7 bleiben über den ganzen Sweep fest (siehe --sweep-r357)."""
    def __init__(self, values, dwell, r357, log_path):
        self.values = values
        self.dwell = dwell
        self.r357 = r357  # (r3, r5, r7)
        self.log_path = log_path
        self.index = 0
        self.value_start = None
        self.samples = []  # (t_since_start, current, power)
        self.results = []
        self.done = False

    def current_regs(self):
        v = self.values[self.index]
        r3, r5, r7 = self.r357
        return [0, v, 0, r3, 0, r5, 0, r7, 0]

    def start_if_needed(self, now):
        if self.value_start is None:
            self.value_start = now
            self.samples = []
            print(f"\n[{time.strftime('%H:%M:%S')}] === Sweep-Schritt {self.index+1}/{len(self.values)}: R1={self.values[self.index]} für {self.dwell:.0f}s ===")

    def feed_sample(self, now, regs):
        if self.done or self.value_start is None:
            return
        if len(regs) != 19:
            # Das ist die Discovery-Antwort (38 Register, ID-String) oder
            # etwas anderes — nur der bekannte 19-Register-Telemetrie-Block
            # (0x0091) ist hier relevant.
            return
        t_rel = now - self.value_start
        current = regs[1]
        power = regs[3]
        self.samples.append((t_rel, current, power))

    def maybe_advance(self, now):
        """Gibt True zurück, wenn gerade ein Wert abgeschlossen und der
        nächste begonnen wurde (oder der Sweep fertig ist)."""
        if self.done or self.value_start is None:
            return False
        if now - self.value_start < self.dwell:
            return False

        # Stabile zweite Hälfte auswerten
        stable = [s for s in self.samples if s[0] >= self.dwell * 0.5]
        if stable:
            avg_i = sum(s[1] for s in stable) / len(stable)
            avg_p = sum(s[2] for s in stable) / len(stable)
            min_p = min(s[2] for s in stable)
            max_p = max(s[2] for s in stable)
        else:
            avg_i = avg_p = min_p = max_p = float('nan')

        result = {
            'r1_geschrieben': self.values[self.index],
            'avg_strom': avg_i,
            'avg_leistung': avg_p,
            'min_leistung': min_p,
            'max_leistung': max_p,
            'n_samples': len(stable),
        }
        self.results.append(result)
        print(f"[{time.strftime('%H:%M:%S')}] Ergebnis R1={self.values[self.index]}: "
              f"Ø Strom={avg_i:.1f}, Ø Leistung={avg_p:.0f}W (min={min_p:.0f}, max={max_p:.0f}, n={len(stable)})")

        self.index += 1
        self.value_start = None
        if self.index >= len(self.values):
            self.done = True
            self._write_log()
            print(f"\n=== SWEEP FERTIG === Ergebnisse in {self.log_path} gespeichert.\n")
            print(f"{'R1 geschrieben':>15} | {'Ø Strom':>8} | {'Ø Leistung':>10}")
            for r in self.results:
                print(f"{r['r1_geschrieben']:>15} | {r['avg_strom']:>8.1f} | {r['avg_leistung']:>10.0f}")
        return True

    def _write_log(self):
        try:
            with open(self.log_path, 'w') as f:
                f.write("r1_geschrieben,avg_strom,avg_leistung,min_leistung,max_leistung,n_samples\n")
                for r in self.results:
                    f.write(f"{r['r1_geschrieben']},{r['avg_strom']:.2f},{r['avg_leistung']:.1f},"
                            f"{r['min_leistung']:.1f},{r['max_leistung']:.1f},{r['n_samples']}\n")
        except Exception as e:
            print(f"[WARN] Konnte Sweep-Log nicht schreiben: {e}", file=sys.stderr)


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


def build_read_request(unit_id: int, addr: int, qty: int) -> bytes:
    body = bytes([unit_id, 0x03]) + addr.to_bytes(2, 'big') + qty.to_bytes(2, 'big')
    return with_crc(body)


def build_write_ack(unit_id: int, start_addr: int, qty: int) -> bytes:
    body = bytes([unit_id, 0x10]) + start_addr.to_bytes(2, 'big') + qty.to_bytes(2, 'big')
    return with_crc(body)


def build_write_request(unit_id: int, start_addr: int, regs: list) -> bytes:
    """Baut einen Write-Multiple-Registers-Request, den WIR (als B3/Master)
    an die Wallbox senden — Format identisch zu dem, was wir historisch von
    der Wallbox empfangen haben (9 Register ab 0x0100)."""
    data = bytearray()
    for r in regs:
        data += int(r).to_bytes(2, 'big', signed=False)
    body = bytes([unit_id, 0x10]) + start_addr.to_bytes(2, 'big') + len(regs).to_bytes(2, 'big') + bytes([len(data)]) + bytes(data)
    return with_crc(body)


def try_extract_frame(buf: bytes, unit_id: int):
    """Erkennt entweder eine Read-Holding-Registers-ANTWORT (auf unsere
    eigene Anfrage) oder eine eingehende Write-Multiple-Registers-ANFRAGE
    der Wallbox. Rückgabe: (status, payload)."""
    n = len(buf)
    if n < 1:
        return 'incomplete', None
    if buf[0] != unit_id:
        return 'discard1', None
    if n < 2:
        return 'incomplete', None
    func = buf[1]

    if func == 0x03:
        # Antwort auf unser eigenes Read: addr,func,byteCount,...,crc
        if n < 3:
            return 'incomplete', None
        bc = buf[2]
        total = 3 + bc + 2
        if n < total:
            return 'incomplete', None
        frame = buf[:total]
        if modbus_crc16(frame[:-2]) != (frame[-2] | (frame[-1] << 8)):
            return 'discard1', None
        data = frame[3:3 + bc]
        regs = [int.from_bytes(data[i:i + 2], 'big') for i in range(0, len(data), 2)]
        return 'ok', (total, 'read_response', regs)

    if func == 0x10:
        # Zwei Möglichkeiten: (a) kurzes Echo als Antwort auf UNSEREN Write
        # (addr+qty, 8 Bytes gesamt), oder (b) ein längerer, unaufgeforderter
        # Write-Request der Wallbox (addr+qty+byteCount+Daten). Erst das
        # kurze Echo versuchen (kommt nach unseren eigenen Writes), dann
        # den langen Fall.
        if n >= 8:
            short = buf[:8]
            if modbus_crc16(short[:-2]) == (short[-2] | (short[-1] << 8)):
                addr = (short[2] << 8) | short[3]
                qty = (short[4] << 8) | short[5]
                return 'ok', (8, 'write_ack', (addr, qty))
        if n < 7:
            return 'incomplete', None
        bc = buf[6]
        total = 7 + bc + 2
        if n < total:
            return 'incomplete', None
        frame = buf[:total]
        if modbus_crc16(frame[:-2]) != (frame[-2] | (frame[-1] << 8)):
            return 'discard1', None
        addr = (frame[2] << 8) | frame[3]
        qty = (frame[4] << 8) | frame[5]
        data = frame[7:7 + bc]
        return 'ok', (total, 'write_request', (addr, qty, data))

    if func == 0x10 | 0x80 or func == 0x03 | 0x80:
        # Exception-Antwort
        if n < 5:
            return 'incomplete', None
        frame = buf[:5]
        if modbus_crc16(frame[:-2]) != (frame[-2] | (frame[-1] << 8)):
            return 'discard1', None
        return 'ok', (5, 'exception', frame[2])

    return 'discard1', None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--unit-id", type=int, default=1)
    ap.add_argument("--poll-interval", type=float, default=0.2,
                     help="Sekunden zwischen den 0x91-Telemetrie-Reads (Standard: 0.2s, wie beobachtet)")
    ap.add_argument("--discovery-interval", type=float, default=1.0,
                     help="Sekunden zwischen den 0x0001-Discovery-Reads (Standard: 1.0s, wie beobachtet)")
    ap.add_argument("--response-timeout", type=float, default=0.3,
                     help="Wie lange auf eine Antwort gewartet wird, bevor die nächste Anfrage geschickt wird")
    ap.add_argument("--write-interval", type=float, default=0.0,
                     help="Sekunden zwischen Writes in den 0x0100-Block. 0 = Writes deaktiviert (Standard, nur Lesen).")
    ap.add_argument("--write-overrides", default="b3_write_override.json",
                     help="JSON-Datei mit 9 Registerwerten für den Write (Liste), z.B. [0,0,0,0,0,0,0,0,1]")
    ap.add_argument("--sweep", default=None,
                     help="Komma-getrennte Liste von R1-Testwerten für automatische Kartierung, z.B. 800,1500,2900,4500,6000,7500,9000,10500,12000,14000,16000. Überschreibt --write-overrides.")
    ap.add_argument("--sweep-dwell", type=float, default=60.0,
                     help="Sekunden pro Testwert im Sweep (Standard: 60s)")
    ap.add_argument("--sweep-r357", default="4000,7000,7000",
                     help="Feste Werte für R3,R5,R7 während des gesamten Sweeps, komma-getrennt (Standard: 4000,7000,7000)")
    ap.add_argument("--sweep-log", default="sweep_results.csv",
                     help="Datei für die Sweep-Ergebnistabelle (Standard: sweep_results.csv)")
    args = ap.parse_args()

    print("=" * 70)
    print("B3-MASTER-EMULATOR — SICHERHEITSHINWEIS")
    print("Nur verwenden, wenn die echte B3 physisch vom Bus getrennt ist!")
    print("Zuerst OHNE angestecktes Fahrzeug testen.")
    print("=" * 70)

    ser = serial.Serial(
        port=args.port, baudrate=args.baud,
        bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE, stopbits=1,
        timeout=0.02,
    )

    sweep = None
    if args.sweep:
        try:
            values = [int(v.strip()) for v in args.sweep.split(',') if v.strip()]
            r357 = tuple(int(v.strip()) for v in args.sweep_r357.split(','))
            if len(r357) != 3:
                sys.exit("--sweep-r357 braucht genau 3 Werte (R3,R5,R7)")
        except ValueError:
            sys.exit("--sweep und --sweep-r357 müssen komma-getrennte Ganzzahlen sein")
        sweep = SweepController(values, args.sweep_dwell, r357, args.sweep_log)
        args.write_interval = args.write_interval or 2.0  # Sweep braucht aktives Schreiben
        print(f"SWEEP-MODUS aktiv: {len(values)} Werte, je {args.sweep_dwell:.0f}s, R3/R5/R7 fix={r357}")
        print(f"Geschätzte Gesamtdauer: {len(values)*args.sweep_dwell/60:.1f} Minuten\n")

    print(f"\nB3-Master-Emulator auf {args.port} @ {args.baud} 8N1, Ziel-Unit-ID {args.unit_id}")
    print(f"Poll-Intervall (0x0091): {args.poll_interval}s")
    print(f"Discovery-Intervall (0x0001): {args.discovery_interval}s")
    if args.write_interval > 0:
        print(f"Write-Intervall (0x0100): {args.write_interval}s (Inhalt aus {args.write_overrides}, Default: Leerlauf-Muster)")
    else:
        print("Write in 0x0100: DEAKTIVIERT (nur Lesen). Mit --write-interval aktivieren.")
    print()

    write_store = OverrideStore(args.write_overrides) if (args.write_interval > 0 and not sweep) else None

    buf = bytearray()
    last_poll = 0.0
    last_discovery = 0.0
    last_write = 0.0
    poll_count = 0
    discovery_count = 0
    response_count = 0
    write_sent_count = 0
    write_ack_count = 0
    write_count = 0
    timeout_count = 0
    awaiting_response_since = None

    try:
        while True:
            now = time.monotonic()

            data = ser.read(256)
            if data:
                buf.extend(data)

            # Antworten/Writes verarbeiten
            while buf:
                status, payload = try_extract_frame(bytes(buf), args.unit_id)
                if status == 'incomplete':
                    break
                if status == 'discard1':
                    del buf[0]
                    continue

                frame_len, kind, info = payload
                ts = time.strftime("%H:%M:%S")

                if kind == 'read_response':
                    response_count += 1
                    awaiting_response_since = None
                    preview = info[:6]
                    if sweep:
                        sweep.feed_sample(now, info)
                    else:
                        print(f"[{ts}] ANTWORT erhalten: {len(info)} Register, erste: {preview} ({response_count})")

                elif kind == 'write_ack':
                    write_ack_count += 1
                    awaiting_response_since = None
                    print(f"[{ts}] ACK auf unseren Write erhalten (addr=0x{info[0]:04X} qty={info[1]}) ({write_ack_count})")

                elif kind == 'write_request':
                    addr, qty, wdata = info
                    ack = build_write_ack(args.unit_id, addr, qty)
                    ser.write(ack)
                    write_count += 1
                    vals = [int.from_bytes(wdata[i:i + 2], 'big') for i in range(0, len(wdata), 2)]
                    print(f"[{ts}] WRITE von Wallbox empfangen addr=0x{addr:04X} werte={vals} -> ACK gesendet ({write_count})")

                elif kind == 'exception':
                    print(f"[{ts}] Exception-Antwort: Code 0x{info:02X}")
                    awaiting_response_since = None

                del buf[:frame_len]

            # Timeout auf ausstehende Anfrage prüfen
            if awaiting_response_since is not None and (now - awaiting_response_since) > args.response_timeout:
                timeout_count += 1
                awaiting_response_since = None

            # Sweep: Fortschritt prüfen und ggf. zum nächsten Wert wechseln
            if sweep and not sweep.done:
                sweep.start_if_needed(now)
                sweep.maybe_advance(now)
                if sweep.done:
                    break  # Sweep fertig, Programm beenden

            # Nächste Anfrage fällig? (nur wenn wir nicht auf eine Antwort warten)
            if awaiting_response_since is None:
                if args.write_interval > 0 and now - last_write >= args.write_interval:
                    regs = sweep.current_regs() if sweep else write_store.get()
                    req = build_write_request(args.unit_id, 0x0100, regs)
                    ser.write(req)
                    write_sent_count += 1
                    last_write = now
                    awaiting_response_since = now
                    if not sweep:
                        print(f"[{time.strftime('%H:%M:%S')}] -> WRITE 0x0100 gesendet: {regs} ({write_sent_count})")
                elif now - last_discovery >= args.discovery_interval:
                    req = build_read_request(args.unit_id, 0x0001, 38)
                    ser.write(req)
                    discovery_count += 1
                    last_discovery = now
                    last_poll = now  # Discovery zählt auch als "letzte Aktivität"
                    awaiting_response_since = now
                    if not sweep:
                        print(f"[{time.strftime('%H:%M:%S')}] -> DISCOVERY-Anfrage 0x0001 gesendet ({discovery_count})")
                elif now - last_poll >= args.poll_interval:
                    req = build_read_request(args.unit_id, 0x0091, 19)
                    ser.write(req)
                    poll_count += 1
                    last_poll = now
                    awaiting_response_since = now
                    # (kein Print hier, sonst zu viel Output bei 0.2s Intervall)

        if sweep and sweep.done:
            pass  # Zusammenfassung wurde bereits in maybe_advance() ausgegeben

    except KeyboardInterrupt:
        print("\nBeendet.")
    finally:
        print(f"Poll-Anfragen (0x0091) gesendet:      {poll_count}")
        print(f"Discovery-Anfragen (0x0001) gesendet:  {discovery_count}")
        print(f"Antworten erhalten:                    {response_count}")
        print(f"Eigene Writes (0x0100) gesendet:        {write_sent_count}")
        print(f"ACKs auf eigene Writes erhalten:        {write_ack_count}")
        print(f"Writes von Wallbox empfangen:           {write_count}")
        print(f"Timeouts (keine Antwort):               {timeout_count}")
        ser.close()


if __name__ == "__main__":
    main()
